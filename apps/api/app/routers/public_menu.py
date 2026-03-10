from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.services.menu_service import get_menu

router = APIRouter(prefix="/public/menu", tags=["Public Menu"])

@router.get("/")
def read_menu(db: Session = Depends(get_db)):
    return get_menu(db)
