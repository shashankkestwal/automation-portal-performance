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

# EE Definition defaults
DEFAULT_BASE_IMAGE = "registry.redhat.io/ansible-automation-platform/ee-minimal-rhel8:2.18"
COLLECTIONS_CATALOG_LIMIT = 200
COLLECTIONS_CATALOG_OFFSET = 0
COLLECTIONS_PER_EE = 5

# EE definition labels (match portal form)
EE_DESCRIPTION = "testing EE environment"
AUTOCOMPLETE_COLLECTIONS_PATH = "/api/scaffolder/v2/autocomplete/aap-api-cloud/collections"


def _catalog_entities_list(body):
    """Normalize catalog API response to a list of entities."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("items") or []
    return []


def _git_ci_activity_params(entity):
    """
    Derive provider, host, projectPath for GET /api/catalog/ansible/git/ci-activity
    from a git-repository Component entity (best-effort across catalog shapes).
    """
    spec = entity.get("spec") or {}
    meta = entity.get("metadata") or {}
    ann = meta.get("annotations") or {}

    provider = spec.get("scmProvider") or spec.get("provider") or "gitlab"
    if isinstance(provider, str):
        provider = provider.lower()
        if ":" in provider:
            provider = provider.split(":")[-1].lower()

    host = (
        spec.get("hostName")
        or spec.get("host")
        or spec.get("gitlabHost")
        or ann.get("gitlab.com/host")
    )

    project_path = (
        spec.get("projectPath")
        or spec.get("fullProjectPath")
        or spec.get("gitlabProjectPath")
        or ann.get("gitlab.com/project-path")
    )

    remote = (
        spec.get("remoteUrl")
        or spec.get("url")
        or spec.get("clone_url")
        or ""
    )

    if not host:
        if "gitlab.com" in remote:
            host = "gitlab.com"
        elif "github.com" in remote:
            host = "github.com"
            provider = provider or "github"
        else:
            host = "gitlab.com"

    if not project_path and remote:
        m = re.search(r"(?:gitlab\.com|github\.com)[:/]([^/]+/[^/.]+)", remote)
        if m:
            project_path = m.group(1).replace(".git", "")

    if not project_path:
        labels = meta.get("labels") or {}
        grp = labels.get("gitlab.com/group") or labels.get("group")
        repo = labels.get("gitlab.com/repo") or labels.get("repo")
        if grp and repo:
            project_path = f"{grp}/{repo}"

    if not project_path:
        return None

    if host == "github.com":
        provider = "github"

    return {"provider": provider, "host": host, "project_path": project_path}


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
    # dedupe, preserve order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
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
    """
    Simulates a user creating Execution Environment definitions through the Portal.

    User Journey:
    1. Authenticate via OAuth with AAP
    2. Browse EE templates catalog and template details
    3. Load collections (catalog GET; autocomplete POST optional / commented)
    4. View existing components (catalog load)
    5. POST scaffolder task to create an EE definition (optional / commented)

    Scaffolder HTTP APIs touched by this locustfile (path prefix /api/scaffolder/v2):
    - GET  …/templates/{namespace}/template/{name}/parameter-schema  (active)
    - POST …/autocomplete/aap-api-cloud/collections  (commented for load test)
    - POST …/tasks  (commented for load test)

    Locust ``name`` labels use ``[eb.<area>.<kind>]…`` so Prometheus can aggregate
    per flow (see config/prometheus/ee-builder-new.scenario.yaml).
    """

    def on_start(self):
        self.client.verify = False
        self.token = None
        self._last_portal_refresh_at = 0.0
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
        self._last_portal_refresh_at = time.time()

    def _headers(self):
        h = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _json_headers(self):
        """Headers aligned with browser POSTs (curl): JSON body + Origin."""
        h = self._headers()
        h["Content-Type"] = "application/json"
        host = getattr(self, "host", None)
        if host:
            h["Origin"] = host.rstrip("/")
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

    def _post(self, path, name, json_body=None, catch_response=False):
        kwargs = {
            "headers": self._json_headers() if json_body is not None else self._headers(),
            "name": name,
        }
        if json_body is not None:
            kwargs["json"] = json_body
        if catch_response:
            kwargs["catch_response"] = True
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

    def _phase_catalog_browse(self):
        """Browse EE templates in catalog."""
        # Entity facets - get available kinds
        self._get(
            "/api/catalog/entity-facets?facet=kind",
            "[eb.catalog.facets] GET entity-facets (kind)",
        )

        # Fetch all EE templates (ordered by name)
        self._get(
            "/api/catalog/entities?filter=spec.type%3Dexecution-environment%2Ckind%3Dtemplate&order=asc%3Ametadata.name",
            "[eb.catalog.templates] GET EE templates (ordered)",
        )

        # Count total EE templates
        self._get(
            "/api/catalog/entities/by-query?limit=0&filter=spec.type%3Dexecution-environment%2Ckind%3Dtemplate",
            "[eb.catalog.templates] GET EE templates count (total)",
        )

        # Count owned EE templates (if user has ownership)
        if self.owner_ref:
            owned_filter = f"spec.type%3Dexecution-environment%2Ckind%3Dtemplate%2Crelations.ownedBy%3D{self.owner_ref}"
            self._get(
                f"/api/catalog/entities/by-query?limit=0&filter={owned_filter}",
                "[eb.catalog.templates] GET EE templates count (owned)",
            )

    def _phase_template_details(self):
        """Fetch template details and schema."""
        if not self.template_name:
            logger.warning("No template name available; skipping template details phase")
            return

        # Get full template entity
        self._get(
            f"/api/catalog/entities/by-name/template/{self.template_namespace}/{self.template_name}",
            "[eb.catalog.template_entity] GET template entity by name",
        )

        # Get template parameter schema (for form rendering)
        self._get(
            f"/api/scaffolder/v2/templates/{self.template_namespace}/template/{self.template_name}/parameter-schema",
            "[eb.scaffolder.template] GET template parameter schema",
        )

    def _phase_collections_search(self):
        """Search for Ansible collections via autocomplete."""
        if not self.aap_token:
            logger.debug("No AAP token; skipping collections search")
            return

        # POST commented out for load testing (no portal POST traffic).
        # with self._post(
        #     "/api/scaffolder/v2/autocomplete/aap-api-cloud/collections",
        #     "[eb.scaffolder.autocomplete.search] POST autocomplete collections",
        #     json_body={
        #         "token": self.aap_token,
        #         "context": {"searchQuery": "spec.type=ansible-collection"},
        #     },
        #     catch_response=True,
        # ) as resp:
        #     if resp.ok:
        #         resp.success()
        #     else:
        #         resp.failure(f"Autocomplete failed: {resp.status_code}")

    def _fetch_collections_from_catalog(self):
        """Fallback: load collection names from catalog API (by-query)."""
        with self._get(
            f"/api/catalog/entities/by-query?limit={COLLECTIONS_CATALOG_LIMIT}&offset={COLLECTIONS_CATALOG_OFFSET}&filter=kind%3DComponent%2Cspec.type%3Dansible-collection",
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
                resp.success()
            except Exception as exc:
                resp.failure(f"Parse collections error: {exc}")

    def _phase_fetch_collections(self):
        """
        Prefer scaffolder autocomplete (same path as UI) for collection names:
        POST /api/scaffolder/v2/autocomplete/aap-api-cloud/collections
        Fallback: catalog by-query if no AAP token or autocomplete returns nothing.

        POST autocomplete is commented out for load testing — catalog GET only.
        """
        self.available_collections = []

        if not self.aap_token:
            logger.warning("No AAP token; using catalog API for collection names")
            self._fetch_collections_from_catalog()
            return

        # POST commented out for load testing — use catalog by-query for collection names.
        # payload = {
        #     "token": self.aap_token,
        #     "context": {"searchQuery": "spec.type=ansible-collection"},
        # }
        # with self._post(
        #     AUTOCOMPLETE_COLLECTIONS_PATH,
        #     "[eb.scaffolder.autocomplete.list] POST autocomplete collections (list)",
        #     json_body=payload,
        #     catch_response=True,
        # ) as resp:
        #     if not resp.ok:
        #         resp.failure(f"Autocomplete collections failed: {resp.status_code}")
        #     else:
        #         try:
        #             body = resp.json()
        #             parsed = _parse_collection_names_from_autocomplete(body)
        #             if parsed:
        #                 self.available_collections = parsed
        #                 logger.debug(
        #                     "Autocomplete: %s collections for EE creation",
        #                     len(self.available_collections),
        #                 )
        #             resp.success()
        #         except Exception as exc:
        #             resp.failure(f"Parse autocomplete collections error: {exc}")
        # if not self.available_collections:
        #     logger.warning(
        #         "Autocomplete returned no collections; falling back to catalog API",
        #     )
        self._fetch_collections_from_catalog()

    def _phase_components_view(self):
        """View existing EE components and collections."""
        # Ansible Collections (ordered)
        self._get(
            "/api/catalog/entities?filter=kind%3DComponent%2Cspec.type%3Dansible-collection&order=asc%3Ametadata.name",
            "[eb.catalog.collections] GET collections (ordered)",
        )

        # Count collections (matches portal: limit=0 ansible-collection components)
        self._get(
            "/api/catalog/entities/by-query?limit=0&filter=kind%3DComponent%2Cspec.type%3Dansible-collection",
            "[eb.catalog.collections] GET collections count",
        )

        self._phase_git_repository_flow()

        # All components (catalog home view)
        self._get(
            "/api/catalog/entities?filter=kind%3DComponent&order=asc%3Ametadata.name",
            "[eb.catalog.components] GET all components (ordered)",
        )

        # Ansible-tagged entities count
        self._get(
            "/api/catalog/entities/by-query?limit=0&filter=metadata.tags%3Dansible",
            "[eb.catalog.components] GET ansible-tagged entities count",
        )

    def _phase_git_repository_flow(self):
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
        detail_entity = entity
        dr = self._get(detail_path, "[eb.catalog.git] GET git repository entity by metadata.name")
        if dr.ok:
            try:
                djson = dr.json()
                dlist = _catalog_entities_list(djson)
                if dlist:
                    detail_entity = dlist[0]
            except json.JSONDecodeError:
                pass

        params = _git_ci_activity_params(detail_entity)
        if not params:
            logger.debug(
                "Skipping ansible/git/ci-activity (could not derive provider/host/projectPath)",
            )
            return

        prov = quote(str(params["provider"]), safe="")
        host_q = quote(str(params["host"]), safe="")
        proj_q = quote(str(params["project_path"]), safe="")
        ci_path = (
            "/api/catalog/ansible/git/ci-activity"
            f"?provider={prov}&projectPath={proj_q}&host={host_q}&per_page=15"
        )
        self._get(ci_path, "[eb.other.ansible_ci] GET ansible git ci-activity")

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

    def _phase_scaffolder_create(self):
        if not self.template_name:
            logger.warning("No template name; skipping scaffolder create")
            return None

        ns = self.template_namespace
        name = self.template_name

        # EE name: exactly 8 random alphanumeric chars; description fixed per requirements
        ee_file_name = self._generate_random_string(8)
        ee_description = EE_DESCRIPTION

        collections = self._select_random_collections()

        # Mirrors browser POST /api/scaffolder/v2/tasks (curl --data-raw)
        task_body = {
            "templateRef": f"template:{ns}/{name}",
            "values": {
                "baseImage": DEFAULT_BASE_IMAGE,
                "collections": collections,
                "advancedConfiguration": {
                    "specifyRequirements": False,
                    "addBuildSteps": False,
                },
                "tags": ["execution-environment"],
                "publishAndBuild": {
                    "publishToSCM": False,
                    "sourceControlProvider": {},
                },
                "eeFileName": ee_file_name,
                "templateDescription": ee_description,
            },
        }

        if self.aap_token:
            task_body["secrets"] = {"aapToken": self.aap_token}

        # EE definition creation POST disabled for load testing (uncomment block below to restore).
        # with self._post(
        #     "/api/scaffolder/v2/tasks",
        #     "[eb.scaffolder.tasks] POST create EE definition",
        #     json_body=task_body,
        #     catch_response=True,
        # ) as resp:
        #     if resp.ok:
        #         try:
        #             task_id = resp.json().get("id")
        #             logger.debug(
        #                 f"Created EE definition task: {task_id} (name={ee_file_name}, collections={len(collections)})",
        #             )
        #             resp.success()
        #             return task_id
        #         except Exception as exc:
        #             resp.failure(f"Parse task response error: {exc}")
        #             return None
        #     else:
        #         detail = (resp.text or "")[:200]
        #         resp.failure(f"Create EE task failed: {resp.status_code} {detail}")
        #         return None
        return None

    @task
    def ee_builder_workflow(self):
        """
        Authenticate, browse catalog, load collections.
        Portal POST calls (scaffolder autocomplete, EE create) are commented out; GET-only
        toward the portal where possible. AAP OAuth still uses POST on the gateway for login.
        """
        self._phase_auth()
        self._phase_catalog_browse()
        self._phase_template_details()
        self._phase_fetch_collections()
        self._phase_collections_search()
        self._phase_components_view()

        self._phase_scaffolder_create()
