import struct
from dataclasses import dataclass
from typing import Optional, Tuple

MSG_API_KEY_REQUEST = 0x20
MSG_API_KEY_RESPONSE = 0x21
MSG_API_KEY_REVOKE = 0x22
MSG_API_KEY_REVOKE_ACK = 0x23
MSG_API_KEY_STATUS = 0x24
MSG_API_KEY_STATUS_RESP = 0x25
MSG_API_KEY_DELETE = 0x26
MSG_API_KEY_DELETE_ACK = 0x27
RESULT_SUCCESS = 0x00

# Router → edge error codes (API key management)
API_KEY_ERRORS = {
  0x01: "drone already has an active API key",
  0x02: "invalid session token",
  0x03: "invalid expiration hours",
  0x04: "drone not found",
  0x05: "fleet backend unavailable at POST /drones/{uuid}/request-api-key",
}


def api_key_error_message(error_code: int) -> str:
  return API_KEY_ERRORS.get(error_code, f"unknown error (0x{error_code:02x})")


@dataclass
class APIKeyResponse:
  result: int
  error_code: int
  api_key: str = ""
  expires_at: int = 0


@dataclass
class APIKeyStatusResponse:
  has_active_key: int
  status: str
  api_key: str = ""
  created_at: int = 0
  expires_at: int = 0
  user_uuid: str = ""
  user_activated_at: int = 0


def _pack_string_fields(uuid: str, token: str) -> bytes:
  uuid_bytes = uuid.encode("utf-8")
  token_bytes = token.encode("utf-8")
  packet = struct.pack("<H", len(uuid_bytes)) + uuid_bytes
  packet += struct.pack("<H", len(token_bytes)) + token_bytes
  return packet


def serialize_api_key_request(drone_uuid: str, session_token: str, expiration_hours: int) -> bytes:
  packet = bytes([MSG_API_KEY_REQUEST]) + _pack_string_fields(drone_uuid, session_token)
  packet += struct.pack("<H", int(expiration_hours))
  return packet


def serialize_api_key_revoke(drone_uuid: str, session_token: str) -> bytes:
  return bytes([MSG_API_KEY_REVOKE]) + _pack_string_fields(drone_uuid, session_token)


def serialize_api_key_status(drone_uuid: str, session_token: str) -> bytes:
  return bytes([MSG_API_KEY_STATUS]) + _pack_string_fields(drone_uuid, session_token)


def serialize_api_key_delete(drone_uuid: str, session_token: str) -> bytes:
  return bytes([MSG_API_KEY_DELETE]) + _pack_string_fields(drone_uuid, session_token)


def _read_length_string(data: bytes, offset: int) -> Tuple[str, int]:
  length = struct.unpack_from("<H", data, offset)[0]
  offset += 2
  value = data[offset : offset + length].decode("utf-8")
  return value, offset + length


def parse_api_key_response(data: bytes) -> APIKeyResponse:
  if len(data) >= 3 and data[2] == MSG_API_KEY_RESPONSE:
    offset = 2
  elif data and data[0] == MSG_API_KEY_RESPONSE:
    offset = 0
  else:
    raise ValueError(f"invalid API_KEY_RESPONSE type: 0x{data[0]:02x}" if data else "empty packet")

  if data[offset] != MSG_API_KEY_RESPONSE:
    raise ValueError("invalid API_KEY_RESPONSE header")
  offset += 1
  result = data[offset]
  offset += 1
  error_code = data[offset]
  offset += 1
  resp = APIKeyResponse(result=result, error_code=error_code)
  if result != RESULT_SUCCESS:
    return resp

  key_len = struct.unpack_from("<H", data, offset)[0]
  offset += 2
  resp.api_key = data[offset : offset + key_len].decode("utf-8")
  offset += key_len
  resp.expires_at = struct.unpack_from("<Q", data, offset)[0]
  return resp


def parse_api_key_revoke_ack(data: bytes) -> Tuple[int, int]:
  if not data or data[0] != MSG_API_KEY_REVOKE_ACK:
    raise ValueError("invalid API_KEY_REVOKE_ACK")
  result = data[1]
  error_code = data[2] if len(data) > 2 else 0
  return result, error_code


def parse_api_key_status_response(data: bytes) -> APIKeyStatusResponse:
  if not data or data[0] != MSG_API_KEY_STATUS_RESP:
    raise ValueError("invalid API_KEY_STATUS_RESP")

  offset = 1
  has_active_key = data[offset]
  offset += 1
  status, offset = _read_length_string(data, offset)
  api_key, offset = _read_length_string(data, offset)
  resp = APIKeyStatusResponse(has_active_key=has_active_key, status=status, api_key=api_key)

  if has_active_key == 0x01:
    if len(data) >= offset + 8:
      resp.created_at = struct.unpack_from("<Q", data, offset)[0]
      offset += 8
    if len(data) >= offset + 8:
      resp.expires_at = struct.unpack_from("<Q", data, offset)[0]
      offset += 8
    if len(data) >= offset + 2:
      user_uuid, offset = _read_length_string(data, offset)
      resp.user_uuid = user_uuid
    if len(data) >= offset + 8:
      resp.user_activated_at = struct.unpack_from("<Q", data, offset)[0]
  return resp


def parse_api_key_delete_ack(data: bytes) -> Tuple[int, int]:
  if not data or data[0] != MSG_API_KEY_DELETE_ACK:
    raise ValueError("invalid API_KEY_DELETE_ACK")
  result = data[1]
  error_code = data[2] if len(data) > 2 else 0
  return result, error_code
