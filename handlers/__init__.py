"""Роутеры бота."""
from handlers.admin import router as admin_router
from handlers.student import router as student_router

__all__ = ["admin_router", "student_router"]
