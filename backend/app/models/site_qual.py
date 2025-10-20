# app/models/site_qual.py
from sqlalchemy import Column, Integer, ForeignKey, TIMESTAMP, func, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base

class SiteQual(Base):
    __tablename__ = "site_qual"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False, index=True, unique=True)
    # JSON con pares clave→valor: {"qual.has_study_nurse": "Yes", "qual.t1d_patients_u18": 42, ...}
    data = Column(JSONB, nullable=False, default=dict)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    site = relationship("Site", back_populates="qual_data")