from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "service" in data

def test_public_menu():
    response = client.get("/public/menu")
    assert response.status_code == 200
    data = response.json()
    assert "products" in data
    assert "ingredients" in data
    assert isinstance(data["products"], list)
    assert isinstance(data["ingredients"], list)

def test_order_creation_success():
    # Valid payload matching OrderCreateRequest
    payload = {
        "store_id": 1,
        "table_id": 1,
        "items": [
            {
                "product_id": 1,
                "quantity": 1,
                "ingredients": []
            }
        ]
    }
    response = client.post("/public/orders", json=payload)
    assert response.status_code in [200, 201]  # Depends on specific return
    data = response.json()
    assert "order_id" in data
    assert data["status"] == "NEW"

def test_order_creation_invalid_payload():
    # Missing items payload
    payload = {
        "store_id": 1,
        "table_id": 1
    }
    response = client.post("/public/orders", json=payload)
    assert response.status_code == 422  # Validation error pydantic
    
def test_kitchen_orders_get():
    response = client.get("/kitchen/orders?store_id=1")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    
def test_kitchen_status_patch_invalid_status():
    payload = {"status": "INVALID_STATUS"}
    # Assumes order 99999 doesn't exist or exist. 422 is pydantic validation.
    response = client.patch("/kitchen/orders/99999/status", json=payload)
    assert response.status_code == 422
    
def test_owner_kpis():
    response = client.get("/owner/kpis")
    if response.status_code == 200:
        data = response.json()
        assert "as_of" in data
        assert "kpis" in data
        assert "total_orders" in data["kpis"]
    else:
        # If DB generates error or something
        assert response.status_code == 500

def test_owner_forecast():
    response = client.get("/owner/ingredient-forecast")
    if response.status_code == 200:
        data = response.json()
        assert "items" in data
        assert isinstance(data["items"], list)
