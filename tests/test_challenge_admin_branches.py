"""Branch list shortcuts use the existing admin-only rename/archive workflows."""
import html
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from aiogram import types
from reliability_support import load_functions
from services.challenge_content import paginate

MID = '11111111-1111-1111-1111-111111111111'


class BranchShortcutTests(unittest.IsolatedAsyncioTestCase):
    def env(self):
        repo = NS(list_nodes=AsyncMock(return_value=[dict(id=MID,name='علمي',status='active')]),
                  get_node=AsyncMock(return_value=dict(id=MID,name='<علمي>',status='active')),
                  archive_node=AsyncMock(return_value=True))
        return load_functions('handlers/admin/challenges.py',
            ['_btn','_e','_nav_row','show_home','cb_archive_ask','cb_archive_do'],
            types=types,html=html,P='adm_ch:',PAGE_SIZE=8,paginate=paginate,repo=repo,
            KIND_LABEL={'b':('branch','الفرع')},_render=AsyncMock(),
            _guard_enabled_notice=AsyncMock(return_value=''))

    async def test_each_branch_has_rename_and_delete_shortcuts(self):
        e=self.env();await e['show_home'](None)
        row=e['_render'].await_args.args[2][0]
        self.assertEqual([b.callback_data for b in row],
                         [f'adm_ch:b:{MID}:0',f'adm_ch:ren:b:{MID}',f'adm_ch:x:b:{MID}'])

    async def test_delete_requires_confirmation_and_escapes_name(self):
        e=self.env();await e['cb_archive_ask'](NS(data=f'adm_ch:x:b:{MID}'))
        text=e['_render'].await_args.args[1]
        self.assertIn('&lt;علمي&gt;',text);self.assertIn('بالأرشفة',text)
        e['repo'].archive_node.assert_not_awaited()
        buttons=e['_render'].await_args.args[2][0]
        self.assertEqual(buttons[0].callback_data,f'adm_ch:xy:b:{MID}')
        self.assertEqual(buttons[1].callback_data,'adm_ch:home:0')

    async def test_confirm_uses_existing_soft_delete_and_actor_id(self):
        e=self.env();await e['cb_archive_do'](NS(data=f'adm_ch:xy:b:{MID}',from_user=NS(id=42)))
        e['repo'].archive_node.assert_awaited_once_with('branch',MID,42)

    async def test_stale_branch_button_returns_to_list(self):
        e=self.env();e['repo'].get_node.return_value=None
        await e['cb_archive_ask'](NS(data=f'adm_ch:x:b:{MID}'))
        e['repo'].archive_node.assert_not_awaited()
        self.assertIn('إدارة التحديات',e['_render'].await_args.args[1])
