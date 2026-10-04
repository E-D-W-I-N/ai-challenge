"""Global model endpoint; credentials never enter settings or persistence."""
from shared_models import DEFAULT_COMPATIBLE_BASE_URL, validate_url


def settings(store=None):
    return store.load_model_settings() if store is not None else {"compatible_base_url": DEFAULT_COMPATIBLE_BASE_URL}
