from sqlalchemy import Column, Integer, String, Text, ForeignKey, Enum, TIMESTAMP
from sqlalchemy.orm import relationship
from app.database import Base
import enum

class QuestionnaireType(str, enum.Enum):
    profiling = "profiling"
    qualification = "qualification"

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
    section_order = Column(Integer, nullable=False, default=0)
    questionnaire = relationship("Questionnaire", back_populates="sections")
    questions = relationship("Question", back_populates="section")

class Question(Base):
    __tablename__ = "questions"
    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id"))
    question_text = Column(Text)
    question_number = Column(Text)
    subsection = Column(Text)
    question_order = Column(Integer, nullable=False, default=0)
    section = relationship("Section", back_populates="questions")
    responses = relationship("Response", back_populates="question")

class Response(Base):
    __tablename__ = "responses"
    id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"))
    response_text = Column(Text)
    question = relationship("Question", back_populates="responses")