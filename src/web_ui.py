"""Small web UI hosted by the container to drive the mobile-API login.

Why a paste step remains: the Liebherr OAuth client only accepts the custom
scheme ``com.liebherr.hau.smartdevice://auth`` as a redirect URI, so the identity provider cannot
redirect back to a web server. This UI therefore wraps the manual flow — it
starts the login (one click), and after the user signs in on login.liebherr.com
they paste back the ``com.liebherr.hau.smartdevice://auth?code=...`` URL the browser tried to
open. No terminal, works from any browser on the LAN, and the same page shows
whether the mobile mode is authorised and what the last poll saw.

Bind this to the LAN only — like Z-Wave JS UI or the zigbee2mqtt frontend, it
has no authentication of its own.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Any, Callable

import secrets

from aiohttp import web

from mobile_client import MobileAuthError, MobileClient, generate_pkce

LOGGER = logging.getLogger("liebherr2mqtt.webui")

# Pending logins expire so a stale verifier cannot be reused.
PENDING_TTL = 900  # 15 minutes


def _page(title: str, body: str) -> web.Response:
    doc = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 640px; margin: 40px auto; padding: 0 20px; line-height: 1.5; }}
  h1 {{ font-size: 1.4rem; }}
  .card {{ border: 1px solid #8884; border-radius: 10px; padding: 20px; margin: 16px 0; }}
  .ok {{ color: #1a7f37; }} .warn {{ color: #b8860b; }} .err {{ color: #cf222e; }}
  a.button, button {{ display: inline-block; background: #0e3f5c; color: #fff;
         border: 0; border-radius: 8px; padding: 10px 18px; font-size: 1rem;
         text-decoration: none; cursor: pointer; }}
  input[type=text] {{ width: 100%; box-sizing: border-box; padding: 10px;
         font-size: 0.95rem; border: 1px solid #8886; border-radius: 8px; }}
  code {{ background: #8882; padding: 1px 5px; border-radius: 4px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td {{ padding: 4px 8px; border-bottom: 1px solid #8883; }}
  small {{ opacity: 0.75; }}
</style></head><body>
<h1>Liebherr SmartDevice — mode mobile <small>(expérimental)</small></h1>
{body}
<p><small>Passerelle liebherr2mqtt. À n'exposer que sur le réseau local.</small></p>
</body></html>"""
    return web.Response(text=doc, content_type="text/html")


class MobileWebUI:
    """aiohttp app driving the OAuth login and showing mobile-mode status."""

    def __init__(
        self,
        client: MobileClient,
        status_provider: Callable[[], dict[str, Any]],
        host: str = "0.0.0.0",
        port: int = 8099,
    ) -> None:
        self._client = client
        self._status = status_provider
        self._host = host
        self._port = port
        self._pending: dict[str, dict[str, Any]] = {}  # state -> {verifier, ts}
        self._runner: web.AppRunner | None = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._index),
                web.get("/login", self._login),
                web.post("/complete", self._complete),
                web.post("/logout", self._logout),
            ]
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        LOGGER.info("Web UI de connexion disponible sur http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- helpers ------------------------------------------------------------

    def _sweep(self) -> None:
        now = time.time()
        self._pending = {
            s: p for s, p in self._pending.items() if now - p["ts"] < PENDING_TTL
        }

    # -- routes -------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        self._sweep()
        authorised = self._client.is_authorised
        status = self._status()
        flash = request.query.get("msg", "")
        flash_html = f'<p class="ok">{html.escape(flash)}</p>' if flash else ""

        if authorised:
            rows = ""
            for key, label in (
                ("authorised", "Authentifié"),
                ("last_poll", "Dernier relevé"),
                ("last_error", "Dernière erreur"),
            ):
                val = status.get(key)
                if val:
                    rows += f"<tr><td>{label}</td><td>{html.escape(str(val))}</td></tr>"
            alarms = status.get("alarms") or {}
            for label, active in alarms.items():
                cls = "err" if active else "ok"
                txt = "ACTIVE" if active else "—"
                rows += f'<tr><td>{html.escape(label)}</td><td class="{cls}">{txt}</td></tr>'
            body = f"""{flash_html}
<div class="card">
  <p class="ok">✓ Le mode mobile est authentifié.</p>
  <table>{rows}</table>
</div>
<div class="card">
  <p>Besoin de vous reconnecter (mot de passe changé, jeton révoqué) ?</p>
  <a class="button" href="/login">Reconnecter le compte</a>
  <form method="post" action="/logout" style="display:inline; margin-left:10px;">
    <button style="background:#cf222e;">Effacer le jeton</button>
  </form>
</div>"""
            return _page("Mode mobile — authentifié", body)

        # Not authorised yet
        body = f"""{flash_html}
<div class="card">
  <p class="warn">Le mode mobile n'est pas encore authentifié.</p>
  <p><b>Étape 1 —</b> ouvrez la connexion Liebherr (nouvel onglet) et connectez-vous :</p>
  <a class="button" href="/login" target="_blank" rel="noopener">Se connecter à Liebherr</a>
  <p><small>Faites-le sur un PC, dans un navigateur <b>sans</b> l'app Liebherr installée.</small></p>
</div>
<div class="card">
  <p><b>Étape 2 —</b> récupérez le code.</p>
  <p>Après connexion, vous arrivez sur une page « <i>Vous êtes maintenant renvoyé à
  l'application</i> ». Le navigateur a tenté d'ouvrir une URL
  <code>com.liebherr.hau.smartdevice://auth?code=…</code> qu'il ne sait pas ouvrir
  (schéma privé de l'app), donc le code ne s'affiche pas tout seul. Pour le récupérer :</p>
  <ol>
    <li>Sur cette page, faites <b>Ctrl+U</b> (afficher le code source de la page).</li>
    <li><b>Ctrl+F</b> puis cherchez <code>code=</code>.</li>
    <li>Copiez l'URL entière <code>com.liebherr.hau.smartdevice://auth?code=…&amp;state=…</code></li>
  </ol>
  <p><small>Repli si Ctrl+U ne montre rien : ouvrez <code>chrome://history</code>,
  cherchez « smartdevice », l'URL avec le code y figure.</small></p>
</div>
<div class="card">
  <p><b>Étape 3 —</b> revenez sur cet onglet et collez l'URL ci-dessous :</p>
  <form method="post" action="/complete">
    <input type="text" name="redirect" placeholder="com.liebherr.hau.smartdevice://auth?code=..." autofocus>
    <p><button type="submit">Valider la connexion</button></p>
  </form>
  <p><small>⏱️ Le code n'est valable qu'environ une minute : collez-le sans trop tarder
  après l'avoir copié (sinon relancez l'étape 1, la session reste ouverte).</small></p>
</div>"""
        return _page("Mode mobile — connexion", body)

    async def _login(self, request: web.Request) -> web.Response:
        self._sweep()
        verifier, _ = generate_pkce()
        state = secrets.token_urlsafe(16)
        self._pending[state] = {"verifier": verifier, "ts": time.time()}
        url = self._client.build_authorize_url(verifier, state)
        # Send the browser to Liebherr; the paste box is already shown on return.
        raise web.HTTPFound(url)

    async def _complete(self, request: web.Request) -> web.Response:
        data = await request.post()
        redirect = str(data.get("redirect", "")).strip()
        if not redirect:
            raise web.HTTPFound("/?msg=" + _q("Aucune URL collée."))
        try:
            code = MobileClient.extract_code(redirect)
        except MobileAuthError as err:
            return _page("Erreur", f'<div class="card"><p class="err">{html.escape(str(err))}</p>'
                                    '<p><a href="/">Retour</a></p></div>')
        # Match the verifier by state (CSRF + PKCE binding); fall back to the
        # most recent pending login if the pasted URL carries no state.
        state = _state_of(redirect)
        pending = self._pending.pop(state, None) if state else None
        if pending is None and self._pending:
            _, pending = max(self._pending.items(), key=lambda kv: kv[1]["ts"])
            self._pending.clear()
        if pending is None:
            return _page("Erreur", '<div class="card"><p class="err">Aucune connexion en '
                                    'attente. Recommencez depuis le bouton.</p>'
                                    '<p><a href="/">Retour</a></p></div>')
        try:
            await self._client.exchange_code(code, pending["verifier"])
        except MobileAuthError as err:
            return _page("Erreur", f'<div class="card"><p class="err">Échec de l\'échange : '
                                    f'{html.escape(str(err))}</p><p><a href="/">Retour</a></p></div>')
        raise web.HTTPFound("/?msg=" + _q("Compte connecté. Les alarmes vont remonter."))

    async def _logout(self, request: web.Request) -> web.Response:
        try:
            from pathlib import Path

            Path(self._client._store._path).unlink(missing_ok=True)  # noqa: SLF001
        except OSError:
            pass
        self._client._access_token = None  # noqa: SLF001
        raise web.HTTPFound("/?msg=" + _q("Jeton effacé."))


def _q(text: str) -> str:
    from urllib.parse import quote

    return quote(text)


def _state_of(redirect_url: str) -> str | None:
    from urllib.parse import urlparse, parse_qs

    qs = parse_qs(urlparse(redirect_url).query)
    return qs.get("state", [None])[0]
