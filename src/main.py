from __future__ import annotations

import logging
import os
import sys

if __package__ in {None, ""}:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.pipeline_service import run_full_pipeline
else:
    from .pipeline_service import run_full_pipeline

logger = logging.getLogger(__name__)


def main() -> int:
    try:
        run_full_pipeline()
        return 0
    except Exception as exc:  # noqa: BLE001
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        logger.exception("执行失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
