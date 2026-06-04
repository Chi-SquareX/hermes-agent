import os

from google_auth_oauthlib.flow import Flow

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"


def _sanitize_scopes(scopes: list[str]) -> list[str]:
    allowed_prefix = "https://www.googleapis.com/auth/"
    allowed_exact = {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    }
    cleaned: list[str] = []
    for scope in scopes:
        s = scope.strip()
        if not s:
            continue
        if s in allowed_exact or s.startswith(allowed_prefix):
            cleaned.append(s)
    # preserve order, drop duplicates
    return list(dict.fromkeys(cleaned))

def build_google_login_url(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: list[str],
) -> tuple[str, str]:
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    safe_scopes = _sanitize_scopes(scopes)

    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=safe_scopes,
        redirect_uri=redirect_uri,
        autogenerate_code_verifier=False,
    )

    login_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes=False,
        prompt="consent",
        code_challenge_method=None,
    )

    return login_url, state

def verify_google_oauth_callback(
    callback_url: str,
    expected_state: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: list[str],
) -> dict:
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=scopes,
        state=expected_state,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(authorization_response=callback_url)

    creds = flow.credentials
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }
