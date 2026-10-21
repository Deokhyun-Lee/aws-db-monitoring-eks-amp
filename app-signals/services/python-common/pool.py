"""아주 작은 DB 연결 풀 (pymysql·psycopg2 공용).

- 오류가 난 연결은 풀에 돌려놓지 않고 닫는다
- 정상 반납 때도 rollback으로 열린 트랜잭션을 정리한다. 읽기만 하고 반납한 연결이 스냅샷·메타데이터 잠금을
  쥔 채 남으면 다음 사용자가 옛 데이터를 보고, DB 쪽에는 idle-in-transaction이 쌓여 RCA가 읽는 지표까지 오염된다
"""
import queue
from contextlib import contextmanager


def _close_quietly(conn):
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 - 이미 끊긴 연결이면 rollback도 실패한다. 원래 예외를 가리지 않는다
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


class Pool:
    def __init__(self, factory, size=10):
        self._factory = factory
        self._idle = queue.LifoQueue(maxsize=size)

    @contextmanager
    def connection(self):
        try:
            conn = self._idle.get_nowait()
        except queue.Empty:
            conn = self._factory()
        try:
            yield conn
        except Exception:
            _close_quietly(conn)
            raise
        try:
            conn.rollback()
            self._idle.put_nowait(conn)
        except queue.Full:
            conn.close()
        except Exception:  # noqa: BLE001 - 정리 중 끊긴 연결은 버린다
            _close_quietly(conn)
