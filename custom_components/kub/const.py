"""Constants for the KUB integration."""

from datetime import timedelta

DOMAIN = "kub"
DEVICE_SCAN_INTERVAL = timedelta(hours=12)
KUB_COORDINATOR = "kub_coordinator"
CONF_WATER_STATISTICS = "water_statistics"
KUB_API = "kub_api"
KUB_USER = "kub_user"

# Azure AD B2C OAuth2 configuration
B2C_TENANT = "login.kub.org"
B2C_POLICY = "B2C_1_sign_in"
B2C_CLIENT_ID = "806e58e2-5935-4d1e-abce-2d85ea0dd776"
B2C_AUTHORIZE_URL = f"https://{B2C_TENANT}/{B2C_TENANT}/{B2C_POLICY.lower()}/oauth2/v2.0/authorize"
B2C_TOKEN_URL = f"https://{B2C_TENANT}/{B2C_TENANT}/{B2C_POLICY.lower()}/oauth2/v2.0/token"
B2C_SCOPES = [f"openid", "profile", B2C_CLIENT_ID]
