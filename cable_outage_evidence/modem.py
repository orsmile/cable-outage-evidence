"""Select a supported modem API without mistaking rejection for detection."""

from . import cbn, hitron


class AutoClient:
    def __init__(self, base_url, username, password):
        self._args = base_url, username, password
        self.client = hitron.HitronClient(*self._args)

    @property
    def logged_in(self):
        return self.client.logged_in

    def login(self):
        try:
            self.client.login()
        except hitron.ModemUnsupported as original:
            candidate = cbn.CbnClient(*self._args)
            try:
                candidate.login()
            except hitron.ModemUnsupported:
                raise original
            self.client = candidate

    def get(self, endpoint, timeout=None):
        return self.client.get(endpoint, timeout)

    def logout(self):
        self.client.logout()


def create_client(cfg):
    cls = cbn.CbnClient if cfg.modem_type == "cbn" else AutoClient
    # Legacy hitron-coda settings also detect CBN, so existing installations
    # recover without hand-editing their saved configuration.
    return cls(cfg.modem_url, cfg.modem_username, cfg.modem_password)
