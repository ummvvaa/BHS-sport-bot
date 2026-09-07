"""FSM-состояния регистрации ученика."""
from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    lang = State()
    name = State()
    class_num = State()
    phone = State()
    confirm = State()
