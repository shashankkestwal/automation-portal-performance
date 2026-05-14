import json
import re
import time
import logging
import random
import string
from urllib.parse import quote, unquote, unquote_plus

import urllib3
from locust import HttpUser, task, events
from locust.exception import StopUser

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

__version__ = "1.0.0"

AAP_USERNAME = "admin"
TEMPLATE_NAMESPACE = "default"
DEFAULT_EE_TEMPLATE = "ansible-execution-environment-builder-start-from-scratch"

DEFAULT_BASE_IMAGE = "registry.redhat.io/ansible-automation-platform/ee-minimal-rhel8:2.18"
COLLECTIONS_CATALOG_LIMIT = 200
COLLECTIONS_PER_EE = 5

# Catalog namespace for EE definition Component entities (see download_ee_definition.py).
EE_DEFINITION_COMPONENT_NAMESPACE = "default"

# EE definition labels (match portal form)
EE_DESCRIPTION = "testing EE environment"
AUTOCOMPLETE_COLLECTIONS_PATH = "/api/scaffolder/v2/autocomplete/aap-api-cloud/collections"

# GitHub org for publishToSCM=true EE task payloads (sourceControlProvider.org).
EE_SCM_GITHUB_ORG = "test-rhaap-portal"

# Wait after SCM scaffolder success before GET api.github.com/repos/{org}/{repo} (async push).
SCM_GITHUB_VERIFY_DELAY_SECONDS = 10.0

# PAH build / registry — used when SCM image sync to PAH is enabled in publishAndBuild (see below).
# EE_BUILD_REGISTRY_PAH = "Private Automation Hub (PAH)"
# EE_REGISTRY_TLS_VERIFY = True


def _catalog_entities_list(body):
    """Normalize catalog API response to a list of entities."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("items") or []
    return []


def _scaffolder_run_status_from_body(body):
    """Short status label from GET /api/scaffolder/v2/tasks/{id} JSON."""
    if not isinstance(body, dict):
        return None
    return body.get("status")


def _parse_collection_names_from_autocomplete(body):
    """
    Extract ansible collection names (namespace.name) from scaffolder autocomplete JSON.
    Handles common shapes: results[], items[], or nested dicts with name/title/filterValue.
    """
    names = []

    def add_str(s):
        if isinstance(s, str) and len(s) > 1 and "." in s and not s.startswith("http"):
            names.append(s.strip())

    def walk(obj):
        if obj is None:
            return
        if isinstance(obj, str):
            add_str(obj)
            return
        if isinstance(obj, dict):
            for key in (
                "name",
                "collection_fullname",
                "title",
                "label",
                "filterValue",
                "value",
            ):
                v = obj.get(key)
                if isinstance(v, str):
                    add_str(v)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(body)
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _parse_collection_names_from_autocomplete_results(body):
    """
    Extract collection names from POST …/autocomplete/aap-api-cloud/collections response.
    Primary shape: results[].name (portal / browser).
    Falls back to _parse_collection_names_from_autocomplete for other shapes.
    """
    names = []
    if isinstance(body, dict):
        results = body.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    n = item.get("name")
                    if isinstance(n, str) and n.strip():
                        names.append(n.strip())
    if names:
        seen = set()
        out = []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out
    return _parse_collection_names_from_autocomplete(body)


def _task_create_body_for_log(task_body):
    """Deep copy of scaffolder task JSON with secrets redacted for safe logging."""
    out = json.loads(json.dumps(task_body))
    sec = out.get("secrets")
    if isinstance(sec, dict):
        redacted = dict(sec)
        for key in ("aapToken", "USER_OAUTH_TOKEN"):
            if redacted.get(key):
                redacted[key] = "***REDACTED***"
        out["secrets"] = redacted
    return out


@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument("--aap-url", type=str, default="", help="AAP gateway URL")
    parser.add_argument("--aap-password", type=str, default="", help="AAP admin password")
    parser.add_argument(
        "--aap-access-token",
        type=str,
        default="",
        dest="aap_access_token",
        help="Optional AAP OAuth token for scaffolder secrets.aapToken (see Makefile AAP_ACCESS_TOKEN)",
    )
    parser.add_argument(
        "--scaffolder-task-status-delay-seconds",
        type=float,
        default=10.0,
        dest="scaffolder_task_status_delay_seconds",
    )
    parser.add_argument(
        "--ee-template-name",
        type=str,
        default=DEFAULT_EE_TEMPLATE,
        dest="ee_template_name",
        help="EE template name to use for scaffolder tasks",
    )
    parser.add_argument(
        "--github-user-oauth-token",
        type=str,
        default="",
        dest="github_user_oauth_token",
        help="Optional GitHub PAT for scaffolder secrets.USER_OAUTH_TOKEN (matches browser curl)",
    )
    parser.add_argument(
        "--scm-github-verify-delay-seconds",
        type=float,
        default=SCM_GITHUB_VERIFY_DELAY_SECONDS,
        dest="scm_github_verify_delay_seconds",
        help=(
            "After portal checks, wait this long then GET GitHub API to verify SCM repo exists "
            "(ee-builder only; default from SCM_GITHUB_VERIFY_DELAY_SECONDS)"
        ),
    )


def _extract_csrf_from_cookies(session):
    for cookie in session.cookies:
        if cookie.name == "csrftoken":
            return cookie.value
    return None


def _extract_form_fields(html):
    fields = {}
    for m in re.finditer(r'<input[^>]+type="hidden"[^>]*>', html):
        tag = m.group(0)
        nm = re.search(r'name="([^"]+)"', tag)
        vm = re.search(r'value="([^"]*)"', tag)
        if nm:
            fields[nm.group(1)] = vm.group(1) if vm else ""
    return fields


def _extract_auth_data(html):
    def _from_payload(payload):
        resp = payload.get("response", {}) if isinstance(payload, dict) else {}
        identity = resp.get("backstageIdentity", {}).get("identity", {})
        return {
            "token": resp.get("backstageIdentity", {}).get("token"),
            "aap_token": resp.get("providerInfo", {}).get("accessToken"),
            "user_entity_ref": identity.get("userEntityRef", ""),
            "ownership_refs": identity.get("ownershipEntityRefs", []),
        }

    for pattern in (r"decodeURIComponent\('([^']*)'\)", r'decodeURIComponent\("([^"]*)"\)'):
        for m in re.finditer(pattern, html):
            raw = m.group(1)
            for ufn in (unquote, unquote_plus):
                try:
                    payload = json.loads(ufn(raw))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                data = _from_payload(payload)
                if data.get("token") or data.get("aap_token"):
                    return data

    decoder = json.JSONDecoder()
    for anchor in ("backstageIdentity", "\"backstageIdentity\"", "\"response\""):
        idx = html.find(anchor)
        if idx == -1:
            continue
        start = html.rfind("{", 0, idx)
        if start == -1:
            continue
        for _ in range(5):
            try:
                payload, _end = decoder.raw_decode(html[start:])
                data = _from_payload(payload)
                if data.get("token") or data.get("aap_token"):
                    return data
            except json.JSONDecodeError:
                pass
            next_start = html.rfind("{", 0, start)
            if next_start == -1 or next_start == start:
                break
            start = next_start
    return {}


class EEBuilderUser(HttpUser):

    def on_start(self):
        self.client.verify = False
        self.token = None
        self.aap_token = None
        self.username = None
        self.owner_ref = None
        self.available_collections = []

        opts = self.environment.parsed_options
        self.aap_url = opts.aap_url
        self.aap_password = opts.aap_password
        self.template_namespace = TEMPLATE_NAMESPACE
        self.template_name = opts.ee_template_name

        self._do_initial_oauth()
        if not self.token:
            logger.error("OAuth did not yield a portal token; stopping user to avoid 4xx API calls")
            raise StopUser()
        if opts.aap_access_token:
            self.aap_token = opts.aap_access_token

        self.ee_scm_github_org = EE_SCM_GITHUB_ORG.strip()
        self.github_user_oauth_token = (
            getattr(opts, "github_user_oauth_token", None) or ""
        ).strip()

    def _headers(self):
        h = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _json_headers(self):
        """Headers aligned with browser POSTs (curl): JSON body + Origin + Referer."""
        h = self._headers()
        h["Content-Type"] = "application/json"
        h["Accept"] = "application/json"
        host = getattr(self, "host", None)
        if host:
            base = host.rstrip("/")
            h["Origin"] = base
            h["Referer"] = f"{base}/"
        return h

    def _get(self, path, name, params=None, catch_response=False):
        kwargs = {
            "headers": self._headers(),
            "name": name,
        }
        if params:
            kwargs["params"] = params
        if catch_response:
            kwargs["catch_response"] = True
        return self.client.get(path, **kwargs)

    def _post(self, path, name, json_body=None, catch_response=False, **extra):
        kwargs = {
            "headers": self._json_headers() if json_body is not None else self._headers(),
            "name": name,
        }
        if json_body is not None:
            kwargs["json"] = json_body
        if catch_response:
            kwargs["catch_response"] = True
        kwargs.update(extra)
        return self.client.post(path, **kwargs)

    def _do_initial_oauth(self):
        """Complete OAuth flow with AAP."""
        self.client.cookies.clear()
        try:
            auth_url, nonce = self._oauth_start()
            self._oauth_redirect(auth_url)
            csrf = self._oauth_login()
            callback_url = self._oauth_authorize(auth_url, csrf)
            self._oauth_callback(callback_url, nonce)
        except Exception:
            logger.exception("Initial OAuth failed")

    def _oauth_start(self):
        with self.client.get(
            "/api/auth/rhaap/start",
            params={"env": "production", "scope": "read write",
                    "flow": "popup", "origin": self.host},
            allow_redirects=False,
            name="[auth] GET /api/auth/rhaap/start",
            catch_response=True,
        ) as resp:
            if resp.status_code != 302:
                resp.failure(f"Expected 302, got {resp.status_code}")
                raise RuntimeError("Auth start failed")
            resp.success()
        url = resp.headers["Location"]
        nonce = next(
            (c.value for c in self.client.cookies if c.name == "rhaap-nonce"),
            None,
        )
        return url, nonce

    def _oauth_redirect(self, url):
        with self.client.get(
            url, allow_redirects=False,
            name="[auth] GET AAP authorize (unauthed)",
            catch_response=True,
        ) as resp:
            if resp.status_code != 302:
                resp.failure(f"Expected 302, got {resp.status_code}")
                raise RuntimeError("Redirect failed")
            resp.success()

    def _oauth_login(self):
        login_url = f"{self.aap_url}/api/gateway/v1/login/"
        with self.client.get(
            login_url, allow_redirects=False,
            name="[auth] GET AAP login",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Expected 200, got {resp.status_code}")
                raise RuntimeError("Login page failed")
            resp.success()
        csrf = _extract_csrf_from_cookies(self.client)
        if not csrf:
            raise RuntimeError("No CSRF token")
        # AAP gateway login (external OAuth); not a portal /api/* POST — required for tokens.
        with self.client.post(
            login_url,
            data={"username": AAP_USERNAME, "password": self.aap_password},
            headers={"X-CSRFToken": csrf, "Referer": f"{self.aap_url}/"},
            allow_redirects=False,
            name="[auth] POST AAP login",
            catch_response=True,
        ) as resp:
            if resp.status_code != 302:
                resp.failure(f"Login failed: {resp.status_code}")
                raise RuntimeError("Login failed")
            resp.success()
        return _extract_csrf_from_cookies(self.client)

    def _oauth_authorize(self, url, csrf):
        with self.client.get(
            url, allow_redirects=False,
            name="[auth] GET AAP authorize (authed)",
            catch_response=True,
        ) as resp:
            if resp.status_code == 302:
                resp.success()
                return resp.headers["Location"]
            if resp.status_code != 200:
                resp.failure(f"Expected 200/302, got {resp.status_code}")
                raise RuntimeError("Authorize failed")
            resp.success()
            html = resp.text
        fields = _extract_form_fields(html)
        fields["allow"] = "Authorize"
        csrf = _extract_csrf_from_cookies(self.client)
        post_url = f"{self.aap_url}/o/authorize/"
        # AAP OAuth consent POST (external); required to finish OAuth with password grant flow.
        with self.client.post(
            post_url, data=fields,
            headers={"X-CSRFToken": csrf, "Referer": post_url},
            allow_redirects=False,
            name="[auth] POST AAP authorize",
            catch_response=True,
        ) as resp:
            if resp.status_code != 302:
                resp.failure(f"Authorize POST failed: {resp.status_code}")
                raise RuntimeError("Authorize POST failed")
            resp.success()
        return resp.headers["Location"]

    def _oauth_callback(self, url, nonce):
        if nonce:
            self.client.cookies.set("rhaap-nonce", nonce)
        with self.client.get(
            url, allow_redirects=False,
            name="[auth] GET callback",
            catch_response=True,
        ) as resp:
            if resp.status_code >= 400:
                resp.failure(f"Callback failed: {resp.status_code}")
                raise RuntimeError("Callback failed")
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            html = resp.text or ""
            data = _extract_auth_data(resp.text)

            if not data.get("token"):
                resp.failure(
                    "No token in callback "
                    f"(status={resp.status_code}, content_type={content_type}, "
                    f"len={len(html)}, "
                    f"has_decodeURIComponent={'decodeURIComponent' in html}, "
                    f"has_backstageIdentity={'backstageIdentity' in html})"
                )
                raise RuntimeError("Token extraction failed")
            self.token = data["token"]
            self.aap_token = data.get("aap_token")
            ref = data.get("user_entity_ref", "")
            self.username = ref.split("/")[-1] if "/" in ref else ref
            refs = data.get("ownership_refs", [])
            self.owner_ref = refs[0] if refs else ref
            resp.success()

    def _phase_auth(self):
        with self.client.get(
            "/api/auth/rhaap/refresh",
            params={"env": "production"},
            headers=self._headers(),
            name="[auth] GET /api/auth/rhaap/refresh",
            catch_response=True,
        ) as resp:
            if not resp.ok:
                resp.failure(f"Refresh returned {resp.status_code}")
                if resp.status_code in (401, 403):
                    logger.warning("Token refresh unauthorized (%s); re-running OAuth", resp.status_code)
                    self._do_initial_oauth()
                return
            try:
                body = resp.json()
                bs = body.get("backstageIdentity", {})
                new_token = bs.get("token")
                if new_token:
                    self.token = new_token
                ident = bs.get("identity", {})
                ref = ident.get("userEntityRef", "")
                if ref:
                    self.username = ref.split("/")[-1]
                refs = ident.get("ownershipEntityRefs", [])
                if refs:
                    self.owner_ref = refs[0]
                prov = body.get("providerInfo", {})
                new_aap_token = prov.get("accessToken")
                if new_aap_token:
                    self.aap_token = new_aap_token
                resp.success()
            except Exception as exc:
                resp.failure(f"Parse error: {exc}")

    def _phase_ee_definitions_and_templates(self):
        """
        EE builder catalog only: existing EE definition files (Components), then EE scaffolder templates.
        """
        self._get(
            "/api/catalog/entities?filter=kind%3DComponent%2Cspec.type%3Dexecution-environment"
            "&order=asc%3Ametadata.name",
            "[eb.catalog.ee_defs] GET EE definition components (ordered)",
        )
        self._get(
            "/api/catalog/entities?filter=spec.type%3Dexecution-environment%2Ckind%3Dtemplate"
            "&order=asc%3Ametadata.name",
            "[eb.catalog.templates] GET EE templates (ordered)",
        )
        self._get(
            "/api/catalog/entities/by-query?limit=0&filter=spec.type%3Dexecution-environment%2Ckind%3Dtemplate",
            "[eb.catalog.templates] GET EE templates (total)",
        )
        if self.owner_ref:
            owned_filter = (
                "spec.type%3Dexecution-environment%2Ckind%3Dtemplate%2Crelations.ownedBy%3D"
                f"{self.owner_ref}"
            )
            self._get(
                f"/api/catalog/entities/by-query?limit=0&filter={owned_filter}",
                "[eb.catalog.templates] GET EE templates (owned)",
            )

    def _phase_template_details(self):
        """Fetch template details and schema."""
        if not self.template_name:
            logger.warning("No template name available; skipping template details phase")
            return

        self._get(
            f"/api/catalog/entities/by-name/template/{self.template_namespace}/{self.template_name}",
            "[eb.catalog.template_entity] GET template entity by name",
        )

        # Get template parameter schema (for form rendering)
        self._get(
            f"/api/scaffolder/v2/templates/{self.template_namespace}/template/{self.template_name}/parameter-schema",
            "[eb.scaffolder.template] GET template parameter schema",
        )

    def _fetch_collections_from_autocomplete(self):
        payload = {
            "token": self.aap_token,
            "context": {"searchQuery": "spec.type=ansible-collection"},
        }
        with self._post(
            AUTOCOMPLETE_COLLECTIONS_PATH,
            "[eb.scaffolder.autocomplete.list] POST autocomplete collections",
            json_body=payload,
            catch_response=True,
        ) as resp:
            if not resp.ok:
                resp.failure(f"Autocomplete collections failed: {resp.status_code}")
                return False
            try:
                body = resp.json()
                parsed = _parse_collection_names_from_autocomplete_results(body)
                if parsed:
                    self.available_collections = parsed
                    logger.debug(
                        "Autocomplete: %s collections from results[].name (or fallback parse)",
                        len(self.available_collections),
                    )
                    resp.success()
                    return True
                resp.success()
                return False
            except Exception as exc:
                resp.failure(f"Parse autocomplete collections error: {exc}")
                return False

    def _fetch_collections_from_catalog(self):
        """Fallback: load collection names from catalog API (by-query)."""
        offset = getattr(self, "_collections_catalog_offset", 0)
        with self._get(
            f"/api/catalog/entities/by-query?limit={COLLECTIONS_CATALOG_LIMIT}&offset={offset}"
            "&filter=kind%3DComponent%2Cspec.type%3Dansible-collection",
            "[eb.catalog.collections] GET collections catalog (for EE)",
            catch_response=True,
        ) as resp:
            if not resp.ok:
                resp.failure(f"Fetch collections failed: {resp.status_code}")
                return
            try:
                body = resp.json()
                items = body.get("items", [])
                for item in items:
                    spec = item.get("spec", {})
                    collection_name = spec.get("collection_fullname")
                    if collection_name:
                        self.available_collections.append(collection_name)
                logger.debug(
                    "Catalog fallback: %s collections for EE creation",
                    len(self.available_collections),
                )
                self._collections_catalog_offset = offset + COLLECTIONS_CATALOG_LIMIT
                resp.success()
            except Exception as exc:
                resp.failure(f"Parse collections error: {exc}")

    def _phase_fetch_collections(self):
        self.available_collections = []
        self._collections_catalog_offset = 0

        if not self.aap_token:
            logger.warning("No AAP token; using catalog API for collection names")
            self._fetch_collections_from_catalog()
            return

        if self._fetch_collections_from_autocomplete():
            return

        logger.warning(
            "Autocomplete returned no collections; falling back to catalog API",
        )
        self._fetch_collections_from_catalog()

    def _phase_collections_catalog_page(self):
        """Collections catalog page (ordered list), same family as the portal UI."""
        self._get(
            "/api/catalog/entities?filter=kind%3DComponent%2Cspec.type%3Dansible-collection"
            "&order=asc%3Ametadata.name",
            "[eb.catalog.collections] GET collections page (ordered)",
        )

    def _phase_git_repositories(self):
        """List git-repository catalog components, then open one repo (detail by name)."""
        list_path = "/api/catalog/entities?filter=kind%3DComponent%2Cspec.type%3Dgit-repository"
        resp = self._get(list_path, "[eb.catalog.git] GET git repositories")
        if not resp.ok:
            return
        try:
            raw = resp.json()
        except json.JSONDecodeError:
            logger.warning("Git repositories list: invalid JSON")
            return

        entities = _catalog_entities_list(raw)
        if not entities:
            logger.debug("No git-repository entities in catalog; skipping drill-down")
            return

        entity = entities[0]
        name = (entity.get("metadata") or {}).get("name")
        if not name:
            logger.debug("First git-repository entity has no metadata.name; skipping drill-down")
            return

        enc = quote(str(name), safe="")
        detail_path = (
            "/api/catalog/entities?filter="
            f"metadata.name%3D{enc}%2Ckind%3DComponent%2Cspec.type%3Dgit-repository"
        )
        self._get(detail_path, "[eb.catalog.git] GET git repository entity by metadata.name")

    def _generate_random_string(self, length=8):
        """Generate random string for EE names."""
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

    def _select_random_collections(self):
        """Select collections for EE definition (first N from collections API)."""
        if not self.available_collections:
            return [
                {"name": "amazon.aws"},
                {"name": "ansible.posix"}
            ]

        selected = self.available_collections[:COLLECTIONS_PER_EE]
        return [{"name": c} for c in selected]

    def _create_ee_definition_task(self, use_scm=False):
        if not self.template_name:
            return None

        if use_scm:
            if not self.ee_scm_github_org:
                logger.warning(
                    "Skipping SCM EE create: EE_SCM_GITHUB_ORG is empty",
                )
                return None
            if not self.github_user_oauth_token:
                logger.error(
                    "Skipping SCM EE create: pass --github-user-oauth-token "
                    "(secrets.USER_OAUTH_TOKEN)",
                )
                return None

        ns = self.template_namespace
        name = self.template_name
        ee_file_name = self._generate_random_string(8)
        ee_description = EE_DESCRIPTION
        collections = self._select_random_collections()

        if use_scm:
            publish_and_build = {
                "publishToSCM": True,
                "sourceControlProvider": {
                    "provider": "github",
                    "providerLabel": "Github",
                    "org": self.ee_scm_github_org,
                    "repoName": ee_file_name,
                    "repoExists": False,
                },
                "buildExecutionEnvironment": False,
                # --- PAH registry / EE image sync (disabled; set True and uncomment below to re-enable) ---
                # "buildRegistry": "Private Automation Hub (PAH)",
                # "registryTlsVerify": True,
                # "buildImageName": ee_file_name,
                # "buildImageTag": ee_file_name,
            }
            task_label = "[eb.scaffolder.tasks] POST create EE definition (SCM)"
            tags = ["execution-environment", "scm", ee_file_name]
            advanced_configuration = {
                "specifyRequirements": False,
                "addBuildSteps": False,
            }
            collections_for_task = (
                collections[:1] if collections else [{"name": "amazon.aws"}]
            )
            ee_description = f"{EE_DESCRIPTION} [SCM] {ee_file_name}"
        else:
            publish_and_build = {
                "publishToSCM": False,
                "sourceControlProvider": {
                    "provider": "github",
                    "providerLabel": "Github",
                    "org": "",
                    "repoName": "",
                    "repoExists": False,
                },
                "buildExecutionEnvironment": False,
            }
            task_label = "[eb.scaffolder.tasks] POST create EE definition (non-SCM)"
            tags = ["execution-environment", "non-scm", ee_file_name]
            advanced_configuration = {
                "specifyRequirements": False,
                "addBuildSteps": False,
                "additionalBuildSteps": [],
                "pythonRequirements": [],
                "systemPackages": [],
            }
            collections_for_task = collections

        task_body = {
            "templateRef": f"template:{ns}/{name}",
            "values": {
                "baseImage": DEFAULT_BASE_IMAGE,
                "collections": collections_for_task,
                "advancedConfiguration": advanced_configuration,
                "tags": tags,
                "publishAndBuild": publish_and_build,
                "eeFileName": ee_file_name,
                "templateDescription": ee_description,
            },
        }

        secrets = {}
        if self.aap_token:
            secrets["aapToken"] = self.aap_token
        if self.github_user_oauth_token:
            secrets["USER_OAUTH_TOKEN"] = self.github_user_oauth_token
        if secrets:
            task_body["secrets"] = secrets

        try:
            logger.info(
                "POST /api/scaffolder/v2/tasks body (%s): %s",
                "SCM" if use_scm else "non-SCM",
                json.dumps(_task_create_body_for_log(task_body)),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Could not serialize scaffolder task body for logging: %s", exc)

        with self._post(
            "/api/scaffolder/v2/tasks",
            task_label,
            json_body=task_body,
            catch_response=True,
        ) as resp:
            if resp.ok:
                try:
                    data = resp.json() if resp.text else {}
                except json.JSONDecodeError as exc:
                    resp.failure(f"Parse task response error: {exc}")
                    return None
                if isinstance(data, dict) and data.get("error"):
                    resp.failure(f"Scaffolder error in body: {data.get('error')}")
                    return None
                task_id = None
                if isinstance(data, dict):
                    task_id = data.get("id") or data.get("taskId")
                if not task_id:
                    logger.warning(
                        "Scaffolder POST ok but no task id in JSON (name=%s): %s",
                        ee_file_name,
                        data,
                    )
                logger.debug(
                    "Created EE definition task: %s (name=%s, collections=%s, scm=%s)",
                    task_id,
                    ee_file_name,
                    len(collections_for_task),
                    use_scm,
                )
                resp.success()
                return {
                    "task_id": task_id,
                    "ee_file_name": ee_file_name,
                    "use_scm": use_scm,
                }
            detail = (resp.text or "")[:800]
            resp.failure(f"Create EE task failed: {resp.status_code} {detail}")
            return None

    def _get_scaffolder_task_status(self, task_id):
        """GET single scaffolder task (run detail), same as portal task drawer / status poll."""
        with self.client.get(
            f"/api/scaffolder/v2/tasks/{task_id}",
            headers=self._headers(),
            name="[eb.scaffolder.tasks] GET task status (pending)",
            catch_response=True,
        ) as resp:
            if not resp.ok:
                detail = (resp.text or "")[:200]
                resp.request_meta["name"] = (
                    f"[eb.scaffolder.tasks] GET task status (http_{resp.status_code})"
                )
                resp.failure(f"Task status GET failed: {resp.status_code} {detail}")
                return
            try:
                body = resp.json()
            except Exception as exc:
                resp.request_meta["name"] = "[eb.scaffolder.tasks] GET task status (parse error)"
                resp.failure(f"Task status JSON error: {exc}")
                return
            status = _scaffolder_run_status_from_body(body)
            resp.request_meta["name"] = (
                f"[eb.scaffolder.tasks] GET task status ({status})"
            )
            resp.success()

    def _phase_view_created_definitions(self, created):
        if not created:
            return
        opts = self.environment.parsed_options
        delay_s = float(
            getattr(opts, "scaffolder_task_status_delay_seconds", 10.0) or 0.0,
        )
        if delay_s > 0:
            time.sleep(delay_s)

        for meta in created:
            tid = meta.get("task_id")
            ee_name = meta.get("ee_file_name")
            if tid:
                self._get_scaffolder_task_status(tid)
            if ee_name:
                path = (
                    "/api/catalog/entities/by-name/Component/"
                    f"{EE_DEFINITION_COMPONENT_NAMESPACE}/{ee_name}"
                )
                with self.client.get(
                    path,
                    headers=self._headers(),
                    name="[eb.catalog.ee_definition] GET EE definition entity (by name)",
                    catch_response=True,
                ) as resp:
                    if resp.status_code == 404:
                        logger.debug(
                            "EE definition entity not in catalog yet (name=%s)", ee_name
                        )
                        resp.success()
                    elif resp.ok:
                        resp.success()
                    else:
                        detail = (resp.text or "")[:200]
                        resp.failure(
                            f"EE definition entity GET failed: {resp.status_code} {detail}",
                        )

        if self.username:
            self.client.get(
                "/api/scaffolder/v2/tasks",
                headers=self._headers(),
                name="[eb.scaffolder.tasks] GET task history (page 1)",
                params={
                    "createdBy": f"user:default/{self.username}",
                    "limit": 10,
                    "offset": 0,
                },
            )

        for meta in created:
            if not meta.get("use_scm"):
                continue
            repo = (meta.get("ee_file_name") or "").strip()
            org = (self.ee_scm_github_org or "").strip()
            if not repo or not org:
                continue
            self._verify_github_scm_repo_exists(org, repo)

    def _verify_github_scm_repo_exists(self, org: str, repo: str) -> None:
        token = (self.github_user_oauth_token or "").strip()
        if not token:
            logger.warning("Skipping GitHub SCM verify: no --github-user-oauth-token")
            return

        opts = self.environment.parsed_options
        delay_s = float(
            getattr(opts, "scm_github_verify_delay_seconds", SCM_GITHUB_VERIFY_DELAY_SECONDS)
            or 0.0,
        )
        if delay_s > 0:
            time.sleep(delay_s)

        o = quote(org, safe="")
        r = quote(repo, safe="")
        url = f"https://api.github.com/repos/{o}/{r}"
        gh_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        with self.client.get(
            url,
            headers=gh_headers,
            name="[eb.scm.github] GET /repos/:org/:repo (verify SCM EE)",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
                logger.info("GitHub SCM verify OK: repo %s/%s exists", org, repo)
                return
            if resp.status_code == 404:
                resp.failure(
                    f"GitHub SCM verify: repo not found (404) — org={org} repo={repo} "
                    "(task may still be running or publish failed)",
                )
                logger.warning(
                    "GitHub SCM verify 404 for %s/%s (increase --scm-github-verify-delay-seconds?)",
                    org,
                    repo,
                )
                return
            detail = (resp.text or "")[:300]
            resp.failure(
                f"GitHub SCM verify failed: HTTP {resp.status_code} {detail}",
            )

    def _phase_scaffolder_create(self):
        out = []
        meta = self._create_ee_definition_task(use_scm=False)
        if meta:
            out.append(meta)
        meta = self._create_ee_definition_task(use_scm=True)
        if meta:
            out.append(meta)
        return out

    @task
    def ee_builder_workflow(self):
        self._phase_auth()
        self._phase_ee_definitions_and_templates()
        self._phase_auth()
        self._phase_template_details()
        self._phase_fetch_collections()
        self._phase_collections_catalog_page()
        self._phase_git_repositories()
        self._phase_auth()
        created = self._phase_scaffolder_create()
        self._phase_view_created_definitions(created)

