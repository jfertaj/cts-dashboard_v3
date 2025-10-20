import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    GOOGLE_MAPS_API_KEY: str = os.getenv("GOOGLE_MAPS_API_KEY", "")
    GOOGLE_REGION_BIAS: str = os.getenv("GOOGLE_REGION_BIAS", "")
    GEO_MAX_ATTEMPTS: int = int(os.getenv("GEO_MAX_ATTEMPTS", "6"))
    SALESFORCE_API_VERSION: str = os.getenv("SALESFORCE_API_VERSION", "v59.0")

settings = Settings()