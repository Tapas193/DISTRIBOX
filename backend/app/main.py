from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="DISTRIBOX API",
    description="Hotel Channel Intelligence & Revenue Optimization API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "success": True,
        "service": "DISTRIBOX API",
        "version": "1.0.0",
        "status": "healthy",
    }


@app.get("/health")
def health():
    return {
        "success": True,
        "status": "healthy",
    }