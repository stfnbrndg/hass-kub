"""Knoxville Utilities Board API

After B2C OAuth2 login, the auth code is exchanged through KUB's
server-side token proxy which sets HTTP-only session cookies.
All API calls use those cookies for authentication.
"""

import copy
from datetime import datetime, timedelta, timezone
from enum import Enum
from http.cookies import SimpleCookie

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


TOKEN_PROXY_URL = "https://www.kub.org/api/auth/v1/oauth2/v2.0/token/customer"
CLIENT_ID = "806e58e2-5935-4d1e-abce-2d85ea0dd776"


class Http:
    """HTTP client with cookie-based auth."""

    def __init__(self, cookies: dict | None = None) -> None:
        self._session = None
        self._cookies = cookies or {}

    async def __aenter__(self):
        jar = aiohttp.CookieJar()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            cookie_jar=jar,
        )
        # Set stored cookies
        for name, value in self._cookies.items():
            self._session.cookie_jar.update_cookies(
                {name: value},
                response_url=aiohttp.client.URL("https://www.kub.org"),
            )
        return self

    async def __aexit__(self, *err):
        await self._session.close()
        self._session = None

    async def fetch(self, url):
        """HTTP GET with cookie auth."""
        resp = await self._session.get(url)
        if resp.status == 401:
            raise KUBAuthenticationError("Session expired or invalid")
        resp.raise_for_status()
        return resp

    async def post_form(self, url, data):
        """HTTP POST with form data (for token proxy)."""
        form = aiohttp.FormData()
        for k, v in data.items():
            form.add_field(k, v)
        resp = await self._session.post(url, data=form)
        return resp

    def get_cookies(self) -> dict:
        """Extract cookies from the session."""
        cookies = {}
        for cookie in self._session.cookie_jar:
            cookies[cookie.key] = cookie.value
        return cookies


class KubUtility:
    """KUB utilities API client.

    Uses cookie-based session auth obtained through the token proxy.
    """

    def __init__(self, cookies: dict | None = None):
        self.cookies = cookies or {}
        self.username = ""
        self.password = ""
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

    def update_cookies(self, cookies: dict):
        """Update stored cookies."""
        self.cookies.update(cookies)

    @staticmethod
    async def exchange_code(code: str, code_verifier: str, redirect_uri: str) -> dict:
        """Exchange B2C auth code through KUB's token proxy.

        Returns the session cookies set by the proxy.
        """
        data = {
            "code": code,
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }

        async with Http() as http:
            resp = await http.post_form(TOKEN_PROXY_URL, data)
            if resp.status != 200:
                body = await resp.text()
                raise KUBAuthenticationError(
                    f"Token proxy returned {resp.status}: {body}"
                )

            # The proxy sets session cookies — capture them
            cookies = http.get_cookies()
            if not cookies:
                raise KUBAuthenticationError("No session cookies received from token proxy")

            return cookies

    @staticmethod
    async def refresh_session(cookies: dict) -> dict:
        """Refresh the session via the token proxy using existing cookies."""
        data = {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
        }

        async with Http(cookies) as http:
            resp = await http.post_form(TOKEN_PROXY_URL, data)
            if resp.status != 200:
                raise KUBAuthenticationError("Session refresh failed")
            return http.get_cookies()

    async def _retrieve_account_info(self):
        """Retrieve Account Info."""
        if self.account_id == "":
            # Try /users/me first, fall back to checking cookies for username
            response = await self.http.fetch(
                "https://www.kub.org/api/auth/v1/users/me"
            )
            json = await response.json()
            self.person_id = json["person"][0]["id"]
            self.account_id = json["person"][0]["accounts"][0]
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
        async with Http(self.cookies) as self.http:
            await self._retrieve_account_info()
            # Update cookies from session
            self.cookies.update(self.http.get_cookies())

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

        async with Http(self.cookies) as self.http:
            if len(self.person_id) == 0:
                await self._retrieve_account_info()

            for service in self.service_list:
                await self._retrieve_usage(service, start_date=start_date)

            # Update cookies from session
            self.cookies.update(self.http.get_cookies())
        return self.usage

    async def verify_access(self):
        """Verify the session cookies work by fetching account info."""
        async with Http(self.cookies) as self.http:
            await self._retrieve_account_info()
