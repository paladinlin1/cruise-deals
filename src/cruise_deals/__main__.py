"""讓 `python -m cruise_deals` 可以直接執行。"""

from .cli import main

if __name__ == "__main__":
  raise SystemExit(main())
