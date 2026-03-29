from abc import ABC, abstractmethod


class DnsProviderError(RuntimeError):
    pass


class BaseDnsProvider(ABC):
    def __init__(self, account):
        self.account = account

    @abstractmethod
    def validate_credentials(self):
        raise NotImplementedError

    @abstractmethod
    def list_zones(self) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def get_record(self, zone_id: str, *, record_type: str, name: str):
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    def delete_record(self, zone_id: str, record_id: str):
        raise NotImplementedError
