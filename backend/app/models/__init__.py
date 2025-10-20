from .site import Site, ProfilingKV
from .questionnaire import QuestionnaireType, Questionnaire, Section, Question, Response
from .geonames import GeonameCity
# from .legacy_structured import *  # si aún lo usas

# Alias para compatibilidad con código antiguo que intenta "GeoNameCity"
GeoNameCity = GeonameCity