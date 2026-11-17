#!/usr/bin/env python3
"""
Map-OS Client for idt-atendimento bot↔ERP bridge.

Provides JWT authentication with automatic token refresh, token persistence,
and OS CRUD methods for the pilot client SebastianCom/cli-002.

Registro: 2026-11-16 · Autor: assistente · Modelo: opencode
Estado: ativo
Origem: checkpoint da conversa - bot↔ERP bridge para SebastianCom
Continua: wire bot logic (F3) e end-to-end test com SebastianCom
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import requests


class MaposClientError(Exception):
    """Base exception for Map-OS client errors."""

    def __init__(self, message: str, status_code: int | None = None, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class MaposAuthError(MaposClientError):
    """Authentication/authorization error."""

    pass


class MaposNotFoundError(MaposClientError):
    """Resource not found (404)."""

    pass


class MaposClient:
    """
    Map-OS API client with JWT authentication and automatic token refresh.

    Uses client-area endpoints (/api/v1/client/...) for OS operations.
    Token is persisted to disk and refreshed automatically on 401.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        token_file: str | Path,
        timeout: int = 30,
    ):
        """
        Initialize Map-OS client.

        Args:
            base_url: Map-OS base URL (e.g., "http://localhost:8145")
            email: Client email for authentication
            password: Client password for authentication
            token_file: Path to persist JWT token (JSON with access_token, expires_at)
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.token_file = Path(token_file)
        self.timeout = timeout

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "idt-atendimento/1.0",
            }
        )

        self._access_token: str | None = None
        self._expires_at: float = 0

        # Load persisted token if exists
        self._load_token()

    # -------------------------------------------------------------------------
    # Token management
    # -------------------------------------------------------------------------

    def _load_token(self) -> None:
        """Load token from disk if valid."""
        if not self.token_file.exists():
            return
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
            token = data.get("access_token")
            expires_at = data.get("expires_at", 0)
            if token and expires_at > time.time() + 60:  # 60s buffer
                self._access_token = token
                self._expires_at = expires_at
        except (json.JSONDecodeError, OSError, KeyError):
            # Invalid/corrupted token file - will re-authenticate
            pass

    def _save_token(self) -> None:
        """Persist token to disk."""
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "access_token": self._access_token,
                "expires_at": self._expires_at,
            }
            self.token_file.write_text(json.dumps(data), encoding="utf-8")
            # Restrict permissions
            os.chmod(self.token_file, 0o600)
        except OSError:
            # Non-fatal - token just won't persist
            pass

    def _ensure_authenticated(self) -> None:
        """Ensure we have a valid access token, login if needed."""
        if self._access_token and self._expires_at > time.time() + 30:
            return
        self._login()

    def _login(self) -> None:
        """Authenticate and obtain JWT token."""
        url = f"{self.base_url}/api/v1/client/auth"
        payload = {"email": self.email, "password": self.password}

        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise MaposAuthError(f"Falha de rede ao autenticar no Map-OS: {e}") from e

        if resp.status_code == 429:
            raise MaposAuthError("Rate limit excedido no login do Map-OS", status_code=429)
        if resp.status_code == 401:
            raise MaposAuthError("Credenciais inválidas para Map-OS", status_code=401)
        if resp.status_code >= 400:
            raise MaposAuthError(
                f"Erro HTTP {resp.status_code} no login: {resp.text[:200]}",
                status_code=resp.status_code,
                response=resp,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise MaposAuthError(f"Resposta de login inválida (não JSON): {e}") from e

        # Map-OS returns: { "status": true, "message": "Login bem-sucedido",
        #   "result": { "access_token": "<jwt>" } } (no expires_in field).
        result = data.get("result") if isinstance(data, dict) else None
        token = (result or {}).get("access_token") if isinstance(result, dict) else None
        if not token:
            token = data.get("token") or data.get("access_token")
        expires_in = data.get("expires_in", 3600)

        if not token:
            raise MaposAuthError(f"Token não encontrado na resposta: {data}")

        self._access_token = token
        self._expires_at = time.time() + expires_in
        self._save_token()

    def _auth_headers(self) -> dict[str, str]:
        """Return headers with Map-OS client-area auth token.

        Map-OS REST_Controller reads Authorization via $_SERVER["HTTP_AUTHORIZATION"]
        and expects 'Authorization: Bearer <token>' (NOT 'Authorization_Token').
        Token type is validated as TOKEN_TYPE_CLIENT.
        """
        self._ensure_authenticated()
        return {"Authorization": f"Bearer {self._access_token}"}

    def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> requests.Response:
        """Make authenticated request with automatic token refresh on 401."""
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers.update(self._auth_headers())
        kwargs["headers"] = headers
        kwargs.setdefault("timeout", self.timeout)

        resp = self._session.request(method, url, **kwargs)

        # Token expired - refresh and retry once
        if resp.status_code == 401:
            self._login()
            headers.update(self._auth_headers())
            kwargs["headers"] = headers
            resp = self._session.request(method, url, **kwargs)

        return resp

    # -------------------------------------------------------------------------
    # OS Operations (Client Area API)
    # -------------------------------------------------------------------------

    def get_os_detail(self, os_id: int | str) -> dict[str, Any]:
        """
        Get OS details by ID.

        Args:
            os_id: OS ID (numeric)

        Returns:
            OS data dict

        Raises:
            MaposNotFoundError: OS not found (404)
            MaposClientError: Other errors
        """
        resp = self._request("GET", f"/api/v1/client/os/{os_id}")

        if resp.status_code == 404:
            raise MaposNotFoundError(f"OS {os_id} não encontrada", status_code=404)
        if resp.status_code >= 400:
            raise MaposClientError(
                f"Erro ao buscar OS {os_id}: HTTP {resp.status_code} - {resp.text[:200]}",
                status_code=resp.status_code,
                response=resp,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise MaposClientError(f"Resposta inválida (não JSON): {e}") from e

        # Map-OS client area returns: { "status": true, "os": { ... } }
        if isinstance(data, dict) and data.get("os"):
            return data["os"]
        return data

    def create_os(
        self,
        descricao_produto: str,
        defeito: str = "",
        observacoes: str = "",
    ) -> dict[str, Any]:
        """
        Create a new OS in Map-OS.

        Args:
            descricao_produto: Product description (required by Map-OS)
            defeito: Defect description
            observacoes: Additional observations

        Returns:
            Created OS data dict with at least 'id' field

        Raises:
            MaposClientError: Validation or server errors
        """
        if not descricao_produto or not descricao_produto.strip():
            raise MaposClientError("descricaoProduto é obrigatório")

        payload = {
            "descricaoProduto": descricao_produto.strip(),
            "defeito": defeito.strip() if defeito else "",
            "observacoes": observacoes.strip() if observacoes else "",
        }

        resp = self._request("POST", "/api/v1/client/os", json=payload)

        if resp.status_code == 422:
            # Validation errors
            try:
                err_data = resp.json()
                errors = err_data.get("erros") or err_data.get("errors") or err_data.get("message")
                raise MaposClientError(
                    f"Dados inválidos para criar OS: {errors}",
                    status_code=422,
                    response=resp,
                )
            except json.JSONDecodeError:
                raise MaposClientError(
                    f"Erro de validação (422): {resp.text[:200]}",
                    status_code=422,
                    response=resp,
                )
        if resp.status_code >= 400:
            raise MaposClientError(
                f"Erro ao criar OS: HTTP {resp.status_code} - {resp.text[:200]}",
                status_code=resp.status_code,
                response=resp,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise MaposClientError(f"Resposta de criação inválida (não JSON): {e}") from e

        # Map-OS returns: { "status": true, "os": { "id": 123, ... }, "message": "..." }
        # or: { "status": true, "message": "...", "os_id": 12 }
        if isinstance(data, dict) and data.get("os"):
            return data["os"]
        if isinstance(data, dict):
            result = dict(data)
            if "os_id" in result and "id" not in result:
                result["id"] = result["os_id"]
            return result
        return data

    def list_os(self, status: str | None = None, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """
        List client's OSs.

        Args:
            status: Filter by status (Aberto, Faturado, Em Andamento, Aguardando Peças, Cancelado, Finalizado)
            limit: Max results
            offset: Pagination offset

        Returns:
            List of OS dicts
        """
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status

        resp = self._request("GET", "/api/v1/client/os", params=params)

        if resp.status_code >= 400:
            raise MaposClientError(
                f"Erro ao listar OSs: HTTP {resp.status_code} - {resp.text[:200]}",
                status_code=resp.status_code,
                response=resp,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise MaposClientError(f"Resposta inválida (não JSON): {e}") from e

        # Map-OS returns: { "status": true, "os": [ ... ], "pagination": {...} }
        if isinstance(data, dict) and data.get("os"):
            return data["os"]
        if isinstance(data, list):
            return data
        return []

    # -------------------------------------------------------------------------
    # Health/utility
    # -------------------------------------------------------------------------

    def health_check(self) -> bool:
        """Check if Map-OS is reachable and credentials work."""
        try:
            self._ensure_authenticated()
            # Try a lightweight call
            resp = self._request("GET", "/api/v1/client/os", params={"limit": 1})
            return resp.status_code < 400
        except Exception:
            return False


# -----------------------------------------------------------------------------
# Convenience factory (used by app.py)
# -----------------------------------------------------------------------------

def create_mapos_client_from_env() -> MaposClient:
    """
    Create MaposClient from environment variables.

    Expected env vars:
        MAPOS_BASE_URL
        MAPOS_CLIENT_EMAIL
        MAPOS_CLIENT_PASSWORD
        MAPOS_TOKEN_FILE
        MAPOS_REQUEST_TIMEOUT (optional, default 30)
    """
    base_url = os.getenv("MAPOS_BASE_URL")
    email = os.getenv("MAPOS_CLIENT_EMAIL")
    password = os.getenv("MAPOS_CLIENT_PASSWORD")
    token_file = os.getenv("MAPOS_TOKEN_FILE")
    timeout = int(os.getenv("MAPOS_REQUEST_TIMEOUT", "30"))

    missing = []
    if not base_url:
        missing.append("MAPOS_BASE_URL")
    if not email:
        missing.append("MAPOS_CLIENT_EMAIL")
    if not password:
        missing.append("MAPOS_CLIENT_PASSWORD")
    if not token_file:
        missing.append("MAPOS_TOKEN_FILE")

    if missing:
        raise MaposClientError(f"Variáveis de ambiente ausentes: {', '.join(missing)}")

    return MaposClient(
        base_url=base_url,
        email=email,
        password=password,
        token_file=token_file,
        timeout=timeout,
    )