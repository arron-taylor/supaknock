"""SQL-free provider health classification and bounded local wait signalling."""


def provider_outcome(error):
    """Only reviewed static adapter codes affect credentials or account health."""
    if error.code in ('rate_limited', 'quota_exhausted', 'invalid_credentials',
                      'payment_required', 'access_denied', 'model_or_endpoint_unavailable'):
        return error.code
    if error.code in ('timeout', 'provider_unavailable', 'network_error') or (
            error.code == 'unavailable' and error.retryable):
        return 'transient'
    return 'unchanged'


class ProviderPoolWait(Exception):
    """No credential can dispatch now; storage bounds unpaid waiting durably."""
    def __init__(self, retry_at):
        super().__init__('provider_pool_wait')
        self.retry_at = retry_at
