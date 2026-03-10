from sqlalchemy.orm import Session
from app.models.product import Product
from app.models.ingredient import Ingredient

def get_menu(db: Session):
    products = db.query(Product).all()
    ingredients = db.query(Ingredient).all()
    
    return {
        "products": products,
        "ingredients": ingredients
    }
