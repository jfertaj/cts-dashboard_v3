from sqlalchemy import Column, Integer, Float, String, Date
from app.database import Base

class GeonameCity(Base):
    __tablename__ = "geonames_cities"
    geonameid = Column(Integer, primary_key=True)
    name = Column(String)
    asciiname = Column(String)
    alternatenames = Column(String)
    latitude = Column(Float)
    longitude = Column(Float)
    feature_class = Column(String)
    feature_code = Column(String)
    country_code = Column(String)
    cc2 = Column(String)
    admin1_code = Column(String)
    admin2_code = Column(String)
    admin3_code = Column(String)
    admin4_code = Column(String)
    population = Column(Integer)
    elevation = Column(String)
    dem = Column(String)
    timezone = Column(String)
    modification_date = Column(Date)