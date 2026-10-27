import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.config import S3_PROVIDERS, PiggyError, Settings, migrate_host_config
from core.storage import S3Host

SCHEMA = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text("utf-8"))


def defaults(schema):
    return {
        key: defaults(item["items"]) if item["type"] == "object" else item["default"]
        for key, item in schema.items()
    }


class HostConfigTests(unittest.TestCase):
    def test_fresh_schema_keeps_r2_default(self):
        config = defaults(SCHEMA)
        migrate_host_config(config)
        self.assertEqual(config["image_host"]["provider"], "Cloudflare R2")
        self.assertEqual(Settings.from_dict(config).region, "auto")

    def test_legacy_defaults_migrate_once_without_losing_either_backend(self):
        config = defaults(SCHEMA)
        config.update(
            endpoint="https://old.example.com",
            bucket="pigs",
            access_key="old-key",
            secret_key="old-secret",
            public_base_url="https://img.example.com",
            upload_url="https://http.example.com/upload",
            upload_headers='{"Authorization":"Bearer old"}',
        )
        original = copy.deepcopy(config)
        self.assertTrue(migrate_host_config(config))
        self.assertEqual(config["image_host"]["provider"], "其他 S3 兼容存储")
        actual = Settings.from_dict(config)
        self.assertEqual(actual.region, "auto")
        self.assertEqual(actual.endpoint, original["endpoint"])
        self.assertNotIn("secret_key", config)
        self.assertFalse(migrate_host_config(config))
        config["image_host"]["provider"] = "自定义 HTTP"
        self.assertEqual(Settings.from_dict(config).upload_headers, {"Authorization": "Bearer old"})
        # AstrBot 下次加载时可能会再次补上隐藏的旧版默认值。
        config.update({k: v for k, v in defaults(SCHEMA).items() if k not in config})
        self.assertFalse(migrate_host_config(config))
        self.assertEqual(Settings.from_dict(config).provider, "http")

    def test_legacy_http_provider_and_advanced_fields(self):
        config = {
            "provider": "http",
            "upload_mode": "json_base64",
            "success_path": "code",
            "success_value": "200",
        }
        migrate_host_config(config)
        parsed = Settings.from_dict(config)
        self.assertEqual(parsed.provider, "http")
        self.assertEqual(parsed.upload_mode, "json_base64")
        self.assertEqual(parsed.success_value, 200)

    def test_new_provider_choice_and_credentials_win(self):
        config = {
            "endpoint": "https://old.example.com",
            "secret_key": "old",
            "image_host": {
                "provider": "Cloudflare R2",
                "r2": {"endpoint": "https://new.example.com", "secret_key": "new"},
            },
        }
        migrate_host_config(config)
        self.assertEqual(Settings.from_dict(config).secret_key, "new")
        self.assertEqual(config["image_host"]["s3"]["secret_key"], "old")

    def test_partial_new_r2_config_keeps_provider_choice(self):
        config = defaults(SCHEMA)
        config["endpoint"] = "https://old.example.com"
        config["image_host"]["r2"]["secret_key"] = "new-secret"
        migrate_host_config(config)
        self.assertEqual(config["image_host"]["provider"], "Cloudflare R2")
        self.assertEqual(Settings.from_dict(config).secret_key, "new-secret")

    def test_migrated_http_fields_remain_editable_text(self):
        config = {"provider": "http", "upload_headers": {"Authorization": "Bearer token"}}
        migrate_host_config(config)
        host = config["image_host"]["http"]
        for key in ("upload_headers", "upload_fields", "success_value"):
            self.assertIsInstance(host[key], str)
            json.loads(host[key])
        self.assertEqual(
            Settings.from_dict(config).upload_headers, {"Authorization": "Bearer token"}
        )

    def test_switching_preserves_settings_and_ignores_inactive_invalid_json(self):
        config = defaults(SCHEMA)
        host = config["image_host"]
        host["r2"].update(endpoint="https://r2.example.com", secret_key="r2-secret")
        host["http"]["upload_headers"] = "broken json"
        first = Settings.from_dict(config)
        host["provider"] = "兰空 Lsky Pro V2"
        host["lsky"]["authorization"] = "Bearer lsky-secret"
        lsky = Settings.from_dict(config)
        self.assertEqual(lsky.upload_headers, {"Authorization": "Bearer lsky-secret"})
        self.assertEqual(lsky.secret_key, "")
        self.assertEqual(
            (lsky.file_field, lsky.response_url_path, lsky.success_path),
            ("file", "data.links.url", "status"),
        )
        host["provider"] = "Cloudflare R2"
        config["show_image_host"] = True
        self.assertEqual(Settings.from_dict(config), first)
        host["provider"] = "自定义 HTTP"
        with self.assertRaises(PiggyError):
            Settings.from_dict(config)

    def test_schema_only_shows_selected_provider(self):
        group = SCHEMA["image_host"]
        self.assertEqual(group["condition"], {"show_image_host": True})
        self.assertFalse(SCHEMA["show_image_host"]["default"])
        items = group["items"]
        for provider in items["provider"]["options"]:
            visible = [
                key
                for key, item in items.items()
                if item.get("condition", {"provider": provider}) == {"provider": provider}
            ]
            self.assertEqual(len(visible), 2)
        self.assertEqual(len(items["provider"]["options"]), 11)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_storage_provider_passes_its_sdk_parameters(self):
        for provider, (section, region, style, signature) in S3_PROVIDERS.items():
            with self.subTest(provider=provider):
                config = {
                    "image_host": {
                        "provider": provider,
                        section: {
                            "endpoint": "https://storage.example.com",
                            "bucket": "pigs",
                            "access_key": "key",
                            "secret_key": "secret",
                            "public_base_url": "https://img.example.com",
                            "region": region or "actual-region",
                            "addressing_style": "auto",
                        },
                    }
                }
                settings = Settings.from_dict(config)
                settings.check_host()
                client = Mock()
                with patch("boto3.client", return_value=client) as create:
                    host = S3Host(settings)
                    url = await host.upload(b"image", "piggy/assets/a.png", "image/png")
                    await host.close()
                kwargs = create.call_args.kwargs
                self.assertEqual(kwargs["config"].signature_version, signature)
                self.assertEqual(kwargs["config"].s3["addressing_style"], style)
                self.assertEqual(kwargs["region_name"], region or "actual-region")
                self.assertEqual(url, "https://img.example.com/piggy/assets/a.png")

    async def test_required_region_and_oss_addressing(self):
        with self.assertRaises(PiggyError):
            Settings.from_dict({"image_host": {"provider": "AWS S3"}}).check_host()
        settings = Settings.from_dict(
            {"image_host": {"provider": "阿里云 OSS", "oss": {"addressing_style": "path"}}}
        )
        self.assertEqual(settings.addressing_style, "virtual")
