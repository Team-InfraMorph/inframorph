import tempfile
import threading
import unittest
from pathlib import Path

from control_plane.db import Store


class ConcurrentAccessTest(unittest.TestCase):
    def test_reads_and_writes_from_many_threads_do_not_collide(self):
        """화면의 동시 조회(API 스레드)와 배포기 이벤트 쓰기(배경 스레드)가 한 연결을 같이 쓴다."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "cp.db")
            project = store.create_project("https://github.com/o/r", "main", ["local", "aws"])
            dep = store.begin_deploy(project["project_id"])
            errors = []

            def writer():
                for i in range(300):
                    store.add_event(dep, {"i": i})

            def reader():
                try:
                    for _ in range(300):
                        store.get_deployment(dep)
                        store.list_events(dep)
                        store.get_plans(dep)
                        store.list_projects()
                except Exception as exc:  # noqa: BLE001 - 어떤 충돌이든 실패로 기록
                    errors.append(repr(exc))

            threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            store.close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
