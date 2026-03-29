import requests

from dns.services.base import BaseDnsProvider, DnsProviderError


class CloudflareDnsProvider(BaseDnsProvider):
    base_url = "https://api.cloudflare.com/client/v4"

    def _headers(self) -> dict:
        token = (self.account.api_token or "").strip()
        if not token:
            raise DnsProviderError("This Cloudflare account has no API token configured.")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params=None, json=None):
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=self._headers(),
            params=params,
            json=json,
            timeout=20,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DnsProviderError(f"Cloudflare returned a non-JSON response ({response.status_code}).") from exc

        if response.status_code >= 400 or not payload.get("success", False):
            errors = payload.get("errors") or []
            if errors:
                message = "; ".join(str(item.get("message") or item) for item in errors)
            else:
                message = payload.get("message") or response.text or f"HTTP {response.status_code}"
            raise DnsProviderError(message)
        return payload.get("result")

    def validate_credentials(self):
        self._request("GET", "/user/tokens/verify")
        return True

    def list_zones(self) -> list[dict]:
        page = 1
        zones: list[dict] = []
        while True:
            result = self._request("GET", "/zones", params={"page": page, "per_page": 50})
            if isinstance(result, list):
                page_results = result
                total_pages = 1
            else:
                page_results = result.get("result", [])
                total_pages = result.get("result_info", {}).get("total_pages", 1)
            zones.extend(page_results)
            if page >= total_pages:
                break
            page += 1
        return zones

    def get_record(self, zone_id: str, *, record_type: str, name: str):
        results = self._request(
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={"type": record_type, "name": name, "per_page": 1},
        )
        if isinstance(results, list):
            return results[0] if results else None
        result_list = results.get("result", [])
        return result_list[0] if result_list else None

    def upsert_record(
        self,
        zone_id: str,
        *,
        record_type: str,
        name: str,
        content: str,
        proxied: bool = False,
        ttl: int = 1,
    ):
        existing = self.get_record(zone_id, record_type=record_type, name=name)
        payload = {
            "type": record_type,
            "name": name,
            "content": content,
            "proxied": bool(proxied),
            "ttl": ttl or 1,
        }
        if existing:
            return self._request(
                "PUT",
                f"/zones/{zone_id}/dns_records/{existing['id']}",
                json=payload,
            )
        return self._request("POST", f"/zones/{zone_id}/dns_records", json=payload)

    def delete_record(self, zone_id: str, record_id: str):
        self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
        return True
