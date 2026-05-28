from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from src.urolens.models.base import Base

class LabRequest:
    """Utility schema mapping variables directly to public.lab_requests columns"""
    __tablename__ = "lab_requests"