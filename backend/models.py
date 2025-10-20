from sqlalchemy import Column, Integer, Float, String, Text, ForeignKey, Enum, TIMESTAMP
from sqlalchemy.orm import relationship
from app.database import Base
import enum

class Example(Base):
    __tablename__ = "examples"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)

class QuestionnaireType(str, enum.Enum):
    profiling = "profiling"
    qualification = "qualification"

class Site(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True)
    name = Column(Text, unique=True, nullable=False, index=True)
    city = Column(String, nullable=True)
    country = Column(String, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    postcode = Column(String, nullable=True)
    questionnaires = relationship("Questionnaire", back_populates="site")


class Questionnaire(Base):
    __tablename__ = "questionnaires"

    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"))
    file_name = Column(Text)
    version = Column(Text)
    type = Column(Enum(QuestionnaireType), nullable=False)
    upload_date = Column(TIMESTAMP)

    file_hash = Column(String, unique=True, index=True, nullable=True)

    site = relationship("Site", back_populates="questionnaires")
    sections = relationship("Section", back_populates="questionnaire")

class Section(Base):
    __tablename__ = "sections"

    id = Column(Integer, primary_key=True)
    name = Column(Text)
    questionnaire_id = Column(Integer, ForeignKey("questionnaires.id"), index=True)
    section_order = Column(Integer, nullable=False, default=0)  # For ordering sections

    questionnaire = relationship("Questionnaire", back_populates="sections")
    questions = relationship("Question", back_populates="section")

class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id"))
    question_text = Column(Text)
    question_number = Column(Text)
    subsection = Column(Text)  # Holds the subsection information
    question_order = Column(Integer, nullable=False, default=0)  # For ordering questions

    section = relationship("Section", back_populates="questions")
    responses = relationship("Response", back_populates="question")

class Response(Base):
    __tablename__ = "responses"

    id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"))
    response_text = Column(Text)

    question = relationship("Question", back_populates="responses")

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
    modification_date = Column(String)