from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    role: str
    user_id: str


class PatientLoginRequest(BaseModel):
    patient_uid: str
    password: str


class PatientLoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    role: str
    user_id: str
