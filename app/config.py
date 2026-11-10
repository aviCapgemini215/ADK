import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://course_app:course_app_local_only@localhost:5432/course_refunds",
)
PAYMENT_GATEWAY_MODE = os.getenv("PAYMENT_GATEWAY_MODE", "demo").lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "groq/qwen/qwen3.8-27b")
