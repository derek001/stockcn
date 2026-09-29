import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]          # project root
DATA_DIR = Path(os.environ.get("STOCK_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "stock.db"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"

DATA_DIR.mkdir(parents=True, exist_ok=True)
