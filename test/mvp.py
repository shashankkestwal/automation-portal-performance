import json
import re
import time
import logging
from urllib.parse import unquote

import urllib3
from locust import HttpUser, task, events

# Intentional: self.client.verify = False for OpenShift routes; silence per-request warnings.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

AAP_USERNAME = "admin"
TASK_POLL_INTERVAL = 5
TASK_POLL_MAX_WAIT = 300


TEMPLATE_NAMESPACE = "default"

@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument("--aap-url", type=str, default="", help="AAP gateway URL")
    parser.add_argument("--aap-password", type=str, default="", help="AAP admin password")

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
    match = re.search(r"decodeURIComponent\('([^']+)'\)", html)
    if not match:
        return {}
    try:
        payload = json.loads(unquote(match.group(1)))
        resp = payload.get("response", {})
        identity = resp.get("backstageIdentity", {}).get("identity", {})
        return {
            "token": resp.get("backstageIdentity", {}).get("token"),
            "aap_token": resp.get("providerInfo", {}).get("accessToken"),
            "user_entity_ref": identity.get("userEntityRef", ""),
            "ownership_refs": identity.get("ownershipEntityRefs", []),
        }
    except (json.JSONDecodeError, KeyError):
        return {}

class PortalUser(HttpUser):

    def on_start(self):
        self.client.verify = False
        self.token = None
        self.aap_token = None
        self.username = None
        self.owner_ref = None

        opts = self.environment.parsed_options
        self.aap_url = opts.aap_url
        self.aap_password = opts.aap_password
        self.template_namespace = TEMPLATE_NAMESPACE
        self.template_name = None

        self._do_initial_oauth()

    def _headers(self):
        h = {"X-Requested-With": "XMLHttpRequest"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, path, name):
        return self.client.get(path, headers=self._headers(), name=name)

    def _post(self, path, name, json_body=None):
        return self.client.post(
            path, headers=self._headers(), json=json_body, name=name,
        )

    def _do_initial_oauth(self):
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
            params={"env": "production", "scope": "read",
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
        logger.info("login page response %s", resp.text)
        logger.info("login page response %s", resp.text)
        csrf = _extract_csrf_from_cookies(self.client)
        if not csrf:
            raise RuntimeError("No CSRF token")
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
            data = _extract_auth_data(resp.text)
            if not data.get("token"):
                resp.failure("No token in callback")
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
                logger.warning("Token refresh failed, keeping existing token")
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
                resp.success()
            except Exception as exc:
                resp.failure(f"Parse error: {exc}")

    def _phase_catalog(self):
        owner = self.owner_ref or "user:default/unknown"

        self._get(
            "/api/catalog/entity-facets?facet=*",
            "[catalog] GET /api/catalog/entity-facets")

        with self.client.get(
            "/api/catalog/entities?filter=kind=template",
            headers=self._headers(),
            name="[catalog] GET /api/catalog/entities",
            catch_response=True,
        ) as resp:
            if resp.ok:
                try:
                    templates = resp.json()
                    if templates and not self.template_name:
                        first = templates[0]
                        self.template_name = first.get("metadata", {}).get("name")
                        self.template_namespace = first.get("metadata", {}).get("namespace", TEMPLATE_NAMESPACE)
                except (json.JSONDecodeError, IndexError, KeyError):
                    logger.warning("Could not parse template list from catalog")
                resp.success()
            else:
                resp.failure(f"Catalog templates failed: {resp.status_code}")

        self._get(
            f"/api/catalog/entities/by-query?limit=0&filter=kind=template,relations.ownedBy={owner}",
            "[catalog] GET /api/catalog/entities/by-query (owned)")
        self._get(
            "/api/catalog/entities/by-query?limit=0&filter=kind=template",
            "[catalog] GET /api/catalog/entities/by-query (all)")

    def _phase_sync(self):
        self._get(
            "/api/catalog/aap/sync_status",
            "[sync] GET /api/catalog/aap/sync_status")
        self._get(
            "/api/catalog/aap/sync_orgs_users_teams",
            "[sync] GET /api/catalog/aap/sync_orgs_users_teams")
        self._get(
            "/api/catalog/aap/sync_job_templates",
            "[sync] GET /api/catalog/aap/sync_job_templates")

    def _phase_scaffolder(self):
        if not self.template_name:
            logger.error("Scaffolder: no template discovered from catalog, skipping")
            return

        ns = self.template_namespace
        name = self.template_name

        self._get(
            f"/api/scaffolder/v2/templates/{ns}/{name}/parameter-schema",
            "[scaffolder] GET /api/scaffolder/v2/templates/.../parameter-schema")
        self._get(
            f"/api/catalog/entities/by-name/template/{ns}/{name}",
            "[scaffolder] GET /api/catalog/entities/by-name/template/...")

        task_body = {
            "templateRef": f"template:{ns}/{name}",
            "values": {},
        }

        with self.client.post(
            "/api/scaffolder/v2/tasks", headers=self._headers(), json=task_body,
            name="[scaffolder] POST /api/scaffolder/v2/tasks",
            catch_response=True,
        ) as resp:
            if resp.ok:
                task_id = resp.json().get("id")
                resp.success()
            else:
                resp.failure(f"Create task failed: {resp.status_code}")
                task_id = None

        if task_id:
            self._poll_task(task_id)
            self._get(
                f"/api/scaffolder/v2/tasks/{task_id}/eventstream",
                "[scaffolder] GET /api/scaffolder/v2/tasks/{id}/eventstream")
        else:
            logger.error("Scaffolder: task creation failed, skipping poll/stream")

        with self.client.post(
            "/api/scaffolder/v2/tasks", headers=self._headers(), json=task_body,
            name="[scaffolder] POST /api/scaffolder/v2/tasks (cancel-run)",
            catch_response=True,
        ) as resp:
            if resp.ok:
                task_id_2 = resp.json().get("id")
                resp.success()
            else:
                resp.failure(f"Create task failed: {resp.status_code}")
                task_id_2 = None

        if task_id_2:
            self._get(
                f"/api/scaffolder/v2/tasks/{task_id_2}",
                "[scaffolder] GET /api/scaffolder/v2/tasks/{id} (status)")
            self._get(
                f"/api/scaffolder/v2/tasks/{task_id_2}/eventstream",
                "[scaffolder] GET /api/scaffolder/v2/tasks/{id}/eventstream (cancel-run)")
            self._post(
                f"/api/scaffolder/v2/tasks/{task_id_2}/cancel",
                "[scaffolder] POST /api/scaffolder/v2/tasks/{id}/cancel")
        else:
            logger.error("Scaffolder: cancel-run task creation failed, skipping")

    def _phase_history(self):
        user = self.username or "unknown"

        self._get(
            f"/api/scaffolder/v2/tasks?createdBy=user:default/{user}&limit=10&offset=0",
            "[history] GET /api/scaffolder/v2/tasks (page 1)")
        self._get(
            f"/api/scaffolder/v2/tasks?createdBy=user:default/{user}&limit=100&offset=0",
            "[history] GET /api/scaffolder/v2/tasks (all)")
        self._get(
            f"/api/scaffolder/v2/tasks?createdBy=user:default/{user}&limit=10&offset=10",
            "[history] GET /api/scaffolder/v2/tasks (page 2)")


    def _poll_task(self, task_id):
        deadline = time.time() + TASK_POLL_MAX_WAIT
        while time.time() < deadline:
            with self.client.get(
                f"/api/scaffolder/v2/tasks/{task_id}",
                headers=self._headers(),
                name="[scaffolder] GET /api/scaffolder/v2/tasks/{id} (poll)",
                catch_response=True,
            ) as resp:
                if resp.ok:
                    status = resp.json().get("status", "")
                    resp.success()
                    if status in ("completed", "failed", "cancelled"):
                        return status
                else:
                    resp.failure(f"Poll failed: {resp.status_code}")
                    return None
            time.sleep(TASK_POLL_INTERVAL)
        logger.warning("Task %s timed out after %ds", task_id, TASK_POLL_MAX_WAIT)
        return "timeout"

    @task
    def user_journey(self):
        time.sleep(20)
        # self._phase_auth()
        # self._phase_catalog()
        # self._phase_sync()
        # self._phase_scaffolder()
        # self._phase_history()
