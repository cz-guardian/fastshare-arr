import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class ApiKey(unittest.TestCase):
    def test_key_is_generated_once_and_reused(self):
        d = tempfile.mkdtemp()
        os.environ.pop("API_KEY", None)
        os.environ["CONFIG_DIR"] = d
        from fastshare_arr import config
        k1, k2 = config.load().api_key, config.load().api_key
        self.assertEqual(k1, k2)
        self.assertEqual(open(os.path.join(d, "api_key")).read().strip(), k1)
        self.assertEqual(os.stat(os.path.join(d, "api_key")).st_mode & 0o777, 0o600)

    def test_env_key_wins_and_is_not_written(self):
        d = tempfile.mkdtemp()
        os.environ.update({"CONFIG_DIR": d, "API_KEY": "mine"})
        from fastshare_arr import config
        self.assertEqual(config.load().api_key, "mine")
        self.assertFalse(os.path.exists(os.path.join(d, "api_key")))
        os.environ.pop("API_KEY")


if __name__ == "__main__":
    unittest.main()
