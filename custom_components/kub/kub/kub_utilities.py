"""Knoxville Utilities Board API

Authenticates via OAuth2 Bearer token. The access token is obtained
by HA's OAuth2 config flow (Azure AD B2C PKCE) and passed to this class.
"""

import copy
from datetime import datetime, timedelta, timezone
from enum import Enum

import aiohttp


class HTTPError(BaseException):
    """Raised when an HTTP operation fails."""

    def __init__(self, status_code, message) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(self.message, self.status_code)


class KUBAuthenticationError(BaseException):
    """Raised when HTTP login fails."""


class KUBUtilityTypes(Enum):
    """KUB Utility Types"""

    ELECTRICITY = "E"
    GAS = "G"
    WATER = "W"
    WASTEWATER = "WW"


class Http:
    """HTTP client with Bearer token auth."""

    def __init__(self, access_token: str = "") -> None:
        self._session = None
        self._access_token = access_token

    async def __aenter__(self):
        headers = {}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers=headers,
        )
        return self

    async def __aexit__(self, *err):
        await self._session.close()
        self._session = None

    async def fetch(self, url):
        """HTTP GET with auth error handling."""
        resp = await self._session.get(url)
        if resp.status == 401:
            raise KUBAuthenticationError("Access token expired or invalid")
        resp.raise_for_status()
        return resp

    async def post(self, url, payload):
        """HTTP POST with auth error handling."""
        resp = await self._session.post(url, json=payload)
        if resp.status == 401:
            raise KUBAuthenticationError("Access token expired or invalid")
        resp.raise_for_status()
        return resp


class KubUtility:
    """KUB utilities API client.

    Can be initialized with either:
    - access_token (OAuth2 B2C flow, preferred)
    - username + password (legacy, will fail with B2C)
    """

    def __init__(self, username_or_token: str = "", password: str = ""):
        # Support both old (username, password) and new (token) signatures
        if password:
            # Legacy mode — will fail on B2C auth
            self.username = username_or_token
            self.password = password
            self.access_token = ""
        else:
            # OAuth2 mode — token-based
            self.username = ""
            self.password = ""
            self.access_token = username_or_token

        self.person_id = ""
        self.account_id = ""
        self.account = {}
        self.usage = {"electricity": {}, "gas": {}, "water": {}, "wastewater": {}}
        self.monthly_total = {
            "electricity": {"usage": None, "cost": None},
            "gas": {"usage": None, "cost": None},
            "water": {"usage": None, "cost": None},
            "wastewater": {"usage": None, "cost": None},
        }
        self.services = {}
        self.service_list = []
        self.http = None

    def update_token(self, access_token: str):
        """Update the access token (called after refresh)."""
        self.access_token = access_token

    async def _retrieve_access_token(self):
        """Legacy auth — POSTs username/password to old session endpoint.

        This endpoint returns 400 since KUB migrated to B2C.
        Kept for backward compatibility signature but will raise on failure.
        """
        if self.access_token:
            # Already have a token from OAuth2 flow
            return

        payload = {
            "session": {
                "username": self.username,
                "password": self.password,
                "expirationDate": "null",
                "user": "null",
            }
        }
        url = "https://www.kub.org/api/auth/v1/sessions"
        response = await self.http.post(url, payload)
        if response.status == 401:
            raise KUBAuthenticationError("Legacy auth failed")

    async def _retrieve_account_info(self):
        """Retrieve Account Info."""
        if self.account_id == "":
            # Try /users/me first (works with B2C token), fall back to username
            try:
                response = await self.http.fetch(
                    "https://www.kub.org/api/auth/v1/users/me"
                )
            except Exception:
                if self.username:
                    response = await self.http.fetch(
                        "https://www.kub.org/api/auth/v1/users/" + self.username
                    )
                else:
                    raise
            json = await response.json()
            self.person_id = json["person"][0]["id"]
            self.account_id = json["person"][0]["accounts"][0]
            if not self.username:
                self.username = json.get("username", "")
        await self._retrieve_services()

    async def _retrieve_services(self):
        url = (
            "https://www.kub.org/api/cis/v1/accounts/"
            + self.account_id
            + "?include=all"
        )
        response = await self.http.fetch(url)
        json = await response.json()
        self.services = json["service-point"]

        for service in self.services:
            match service["type"]:
                case "E-RES":
                    self.account["electricity"] = service["id"]
                    self.service_list.append(KUBUtilityTypes.ELECTRICITY)
                case "G-RES":
                    self.account["gas"] = service["id"]
                    self.service_list.append(KUBUtilityTypes.GAS)
                case "W/S-RES":
                    self.account["water"] = service["id"]
                    self.account["wastewater"] = service["id"]
                    self.service_list.append(KUBUtilityTypes.WATER)
                    self.service_list.append(KUBUtilityTypes.WASTEWATER)
                case _:
                    raise Exception("An unexpected service ID:", service["id"])
        return self.services

    async def retrieve_account_info(self):
        """Retrieves account info from KUB API."""
        async with Http(self.access_token) as self.http:
            await self._retrieve_access_token()
            await self._retrieve_account_info()

    async def retrieve_access_token(self):
        """Fetches access token (legacy)."""
        async with Http(self.access_token) as self.http:
            await self._retrieve_access_token()

    async def _retrieve_usage(
        self,
        utility_type,
        start_date: str = datetime.today().strftime("%Y-%m-%d"),
        end_date: str = datetime.today().strftime("%Y-%m-%d"),
    ):
        utility = utility_type.name.lower()
        account = self.account[utility]

        if utility_type == KUBUtilityTypes.WASTEWATER:
            water = KUBUtilityTypes.WATER.name.lower()
            self.usage[utility] = copy.deepcopy(self.usage[water])
            self.monthly_total[utility]["usage"] = self.monthly_total[water]["usage"]
            self.monthly_total[utility]["cost"] = self.monthly_total[water]["cost"]
            return self.usage

        url = (
            "https://www.kub.org/api/ami/v1/usage-values"
            + "?endDate="
            + end_date
            + "&personId="
            + self.person_id
            + "&servicePointId="
            + account
            + "&startDate="
            + start_date
            + "&utilityType="
            + utility_type.value
        )

        response = await self.http.fetch(url)
        json = await response.json()
        total = 0.0
        total_cost = 0.0
        date = ""
        usage_data = {}
        for idx, usage in enumerate(json["usage-value"]):
            if len(usage["usageValuesChildren"]) == 0:
                usage_data["id"] = usage["id"]
                usage_data["readDateTime"] = usage["readDateTime"]

                data = json["usage-aggregate"][idx]

                usage_data["utilityUsed"] = data["readValue"]
                usage_data["uom"] = data["uom"]
                usage_data["cost"] = data["cost"]

                time = datetime.fromisoformat(usage["readDateTime"]).strftime(
                    "%H:%M:%S"
                )
                self.usage[utility][date][time] = {}
                self.usage[utility][date][time] = copy.deepcopy(usage_data)

                if (
                    datetime.fromisoformat(usage["readDateTime"]).month
                    == datetime.now().month
                ):
                    total = data["readValue"] + total
                    total_cost = data["cost"] + total_cost
            else:
                date = datetime.fromisoformat(usage["readDateTime"]).strftime(
                    "%Y-%m-%d"
                )
                self.usage[utility][date] = {}

        self.monthly_total[utility]["usage"] = total
        self.monthly_total[utility]["cost"] = total_cost
        return self.usage

    async def retrieve_last_31_days(self):
        """Retrieve all usage for the last 31 days."""
        date = datetime.today() - timedelta(days=31)
        start_date = date.strftime("%Y-%m-%d")

        async with Http(self.access_token) as self.http:
            await self._retrieve_access_token()

            if len(self.person_id) == 0:
                await self._retrieve_account_info()

            for service in self.service_list:
                await self._retrieve_usage(service, start_date=start_date)
        return self.usage

    async def retrieve_monthly_usage(self):
        """Retrieve all usage for the current month."""
        date = datetime.today().replace(day=1).date()
        start_date = date.strftime("%Y-%m-%d")

        async with Http(self.access_token) as self.http:
            await self._retrieve_access_token()

            if len(self.person_id) == 0:
                await self._retrieve_account_info()
            for service in self.service_list:
                await self._retrieve_usage(service, start_date=start_date)
        self.http = None
        return self.usage

    async def retrieve_usage_by_range(
        self,
        start_date: str = datetime.today().strftime("%Y-%m-%d"),
        end_date: str = datetime.today().strftime("%Y-%m-%d"),
    ):
        """Retrieve usage for a date range."""
        async with Http(self.access_token) as self.http:
            await self._retrieve_access_token()

            if len(self.person_id) == 0:
                await self._retrieve_account_info()
            for service in self.service_list:
                await self._retrieve_usage(
                    service, start_date=start_date, end_date=end_date
                )
        self.http = None
        return self.usage

    async def retrieve_monthly_summary(self):
        """Retrieve summary of usage for the current month."""
        start_date = datetime.today().replace(day=1).date().strftime("%Y-%m-%d")

        async with Http(self.access_token) as self.http:
            await self._retrieve_access_token()

            if len(self.person_id) == 0:
                await self._retrieve_account_info()
            for service in self.service_list:
                await self._retrieve_usage(service, start_date=start_date)
        self.http = None
        return self.monthly_total

    async def get_available_services(self):
        """Returns available services for account."""
        async with Http(self.access_token) as self.http:
            await self._retrieve_access_token()

            if len(self.person_id) == 0:
                await self._retrieve_account_info()
        return self.services

    async def verify_access(self):
        """Verify the access token works."""
        async with Http(self.access_token) as self.http:
            await self._retrieve_access_token()
            await self._retrieve_account_info()
