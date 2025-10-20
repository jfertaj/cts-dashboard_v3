from sqlalchemy import Column, Integer, Float, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base

class Site(Base):
    __tablename__ = "sites"
    id = Column(Integer, primary_key=True)
    name = Column(Text, unique=True, nullable=False, index=True)

    # Dirección
    street = Column(Text)
    city = Column(String)
    country = Column(String)
    postcode = Column(String)

    # Enlace a Salesforce (opcional)
    salesforce_account_id = Column(String, index=True, nullable=True)  # 🔗

    # Geocoding
    latitude = Column(Float)
    longitude = Column(Float)
    geocode_precision = Column(String)
    geocode_source = Column(String)
    geocode_last_ts = Column(DateTime, server_default=func.now(), onupdate=func.now())

    questionnaires = relationship("Questionnaire", back_populates="site")

    # NUEVO: 1–1 con los datos aplanados de qualifications
    qual_data = relationship("SiteQual", uselist=False, back_populates="site", cascade="all, delete-orphan")


class ProfilingKV(Base):
    __tablename__ = "profiling_kv"
    id = Column(Integer, primary_key=True, autoincrement=True)
    site_id = Column(Integer, ForeignKey("sites.id"), index=True, nullable=False)
    key = Column(String, index=True, nullable=False)
    value = Column(Text)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    site = relationship("Site")