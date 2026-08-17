import unittest

from app.control.model import registry
from app.dataplane.account import AccountDirectory
from app.dataplane.account.selector import select, set_strategy
from app.dataplane.account.table import make_empty_table
from app.dataplane.reverse.protocol.xai_usage import parse_imagine_quota
from app.dataplane.shared.enums import ModeId, PoolId, StatusId


class ImagineQuotaParserTests(unittest.TestCase):
    def _body(self):
        return {
            'image': {'available': True},
            'imagePro': {
                'available': True,
                'remainingQueries': 4,
                'windowSizeSeconds': 86_400,
                'nextAvailableAt': '2026-08-18T00:00:00Z',
            },
            'imageEdit': {'available': False},
            'video': {
                'available': True,
                'remainingQueries': 2,
                'windowSizeSeconds': 86_400,
            },
            'video720p': {
                'available': True,
                'remainingQueries': 1,
                'windowSizeSeconds': 86_400,
            },
        }

    def test_parses_observed_group_shape(self):
        windows = parse_imagine_quota(self._body(), synced_at=1_000)

        self.assertIsNotNone(windows)
        self.assertEqual(windows[int(ModeId.IMAGE_PRO)].remaining, 4)
        self.assertEqual(windows[int(ModeId.IMAGE_PRO)].total, 0)
        self.assertEqual(windows[int(ModeId.IMAGE_PRO)].window_seconds, 86_400)
        self.assertEqual(windows[int(ModeId.IMAGE_EDIT)].remaining, 0)
        self.assertEqual(windows[int(ModeId.IMAGE_EDIT)].reset_at, 1_000 + 86_400 * 1000)
        self.assertEqual(windows[int(ModeId.VIDEO)].remaining, 2)
        self.assertEqual(windows[int(ModeId.VIDEO_720P)].remaining, 1)

    def test_rejects_incomplete_group(self):
        body = self._body()
        del body['video720p']

        self.assertIsNone(parse_imagine_quota(body, synced_at=1_000))

    def test_rejects_invalid_window_size(self):
        body = self._body()
        body['imagePro']['windowSizeSeconds'] = 0

        self.assertIsNone(parse_imagine_quota(body, synced_at=1_000))


class ImageEditRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        set_strategy('quota')

    def tearDown(self):
        set_strategy('random')

    @staticmethod
    def _append_account(table, token, pool_id, *, image_pro=-1, image_edit=-1):
        table._append_slot(
            token=token,
            pool_id=int(pool_id),
            status_id=int(StatusId.ACTIVE),
            quota_auto=1,
            quota_fast=1,
            quota_expert=1,
            quota_heavy=1,
            quota_grok_4_3=1,
            quota_console=1,
            quota_image_pro=image_pro,
            quota_image_edit=image_edit,
            quota_video=-1,
            quota_video_720p=-1,
            total_auto=1,
            total_fast=1,
            total_expert=1,
            total_heavy=1,
            total_grok_4_3=1,
            total_console=1,
            total_image_pro=4,
            total_image_edit=8,
            total_video=2,
            total_video_720p=1,
            window_auto=7_200,
            window_fast=7_200,
            window_expert=7_200,
            window_heavy=7_200,
            window_grok_4_3=7_200,
            window_console=3_600,
            window_image_pro=86_400,
            window_image_edit=86_400,
            window_video=86_400,
            window_video_720p=86_400,
            reset_auto=0,
            reset_fast=0,
            reset_expert=0,
            reset_heavy=0,
            reset_grok_4_3=0,
            reset_console=0,
            reset_image_pro=0,
            reset_image_edit=0,
            reset_video=0,
            reset_video_720p=0,
            health=1.0,
            last_use_s=0,
            last_fail_s=0,
            fail_count=0,
            tags=[],
        )

    def test_known_exhausted_image_quota_is_not_selected(self):
        table = make_empty_table()
        self._append_account(table, 'exhausted', PoolId.BASIC, image_pro=0)
        self._append_account(table, 'unknown', PoolId.BASIC, image_pro=-1)
        unknown_idx = table.idx_by_token['unknown']

        selected = select(
            table,
            int(PoolId.BASIC),
            int(ModeId.IMAGE_PRO),
            exclude_idxs=None,
            prefer_tag_idxs=None,
            now_s=100,
        )

        self.assertEqual(selected, unknown_idx)

    async def test_basic_image_edit_uses_image_pro_quota(self):
        table = make_empty_table()
        self._append_account(table, 'basic', PoolId.BASIC, image_pro=4, image_edit=0)
        self._append_account(table, 'super', PoolId.SUPER, image_pro=20, image_edit=8)
        directory = AccountDirectory(None)
        directory._table = table

        lease = await directory.reserve_image_edit((int(PoolId.BASIC), int(PoolId.SUPER)), now_s_override=100)

        self.assertIsNotNone(lease)
        self.assertEqual(lease.token, 'basic')
        self.assertEqual(lease.mode_id, int(ModeId.IMAGE_PRO))

    async def test_super_image_edit_uses_image_edit_quota(self):
        table = make_empty_table()
        self._append_account(table, 'super', PoolId.SUPER, image_pro=20, image_edit=8)
        directory = AccountDirectory(None)
        directory._table = table

        lease = await directory.reserve_image_edit((int(PoolId.SUPER),), now_s_override=100)

        self.assertIsNotNone(lease)
        self.assertEqual(lease.token, 'super')
        self.assertEqual(lease.mode_id, int(ModeId.IMAGE_EDIT))

    async def test_video_resolution_selects_matching_quota_window(self):
        table = make_empty_table()
        self._append_account(table, 'video', PoolId.SUPER)
        table.quota_video_by_idx[0] = 2
        table.quota_video_720p_by_idx[0] = 1
        directory = AccountDirectory(None)
        directory._table = table

        standard = await directory.reserve_video((int(PoolId.SUPER),), resolution_name='480p', now_s_override=100)
        await directory.release(standard)
        high = await directory.reserve_video((int(PoolId.SUPER),), resolution_name='720p', now_s_override=100)

        self.assertEqual(standard.mode_id, int(ModeId.VIDEO))
        self.assertEqual(high.mode_id, int(ModeId.VIDEO_720P))

    def test_image_edit_model_is_basic(self):
        spec = registry.get('grok-imagine-image-edit')

        self.assertIsNotNone(spec)
        self.assertEqual(spec.tier.name, 'BASIC')
        self.assertTrue(spec.is_image_edit())


if __name__ == '__main__':
    unittest.main()
