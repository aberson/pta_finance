"""Production launcher. Test trust is never selected from arguments or environment."""

from __future__ import annotations

import logging
import sys
from types import MappingProxyType

import uvicorn
from google.cloud.firestore_v1.services.firestore import FirestoreClient

from .app import create_app
from .auth import IAPVerifier
from .catalog import load_catalog
from .config import load_config
from .models import WorkflowError, load_source
from .store import Store


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise WorkflowError("CONFIG_INVALID")
        config = load_config()
        source = load_source()
        catalog = load_catalog() if config.mode == "queue" else None
        store = None
        catalog_stores = None
        if config.mode != "identity":
            try:
                client = FirestoreClient()
                if catalog is not None:
                    catalog_stores = MappingProxyType(
                        {
                            request_id: Store(config, item, client)
                            for request_id, item in catalog.items()
                        }
                    )
                    store = catalog_stores[source["request_id"]]
                else:
                    store = Store(config, source, client)
            except Exception as exc:
                raise WorkflowError("STORE_UNAVAILABLE") from exc
        app = create_app(config, IAPVerifier(config), store, catalog_stores=catalog_stores)
    except WorkflowError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr, flush=True)
        return 1
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run(app, host="0.0.0.0", port=config.port, access_log=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
