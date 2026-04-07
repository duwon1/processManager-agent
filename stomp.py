"""STOMP 프로토콜 프레임 생성 및 파싱 헬퍼입니다."""


def stomp_frame(command: str, headers: dict, body: str = "") -> str:
    """STOMP 프로토콜 형식의 프레임 문자열을 생성합니다."""
    frame = f"{command}\n"
    for key, value in headers.items():
        frame += f"{key}:{value}\n"
    return frame + "\n" + body + chr(0)


def extract_stomp_body(frame: str) -> str:
    """STOMP MESSAGE 프레임에서 바디 부분을 추출합니다."""
    if "\n\n" not in frame:
        return ""
    return frame.split("\n\n", 1)[1].rstrip("\x00")
