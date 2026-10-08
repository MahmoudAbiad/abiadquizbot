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


class ArchiveWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def env(self):
        from aiogram.fsm.state import State, StatesGroup
        names=['ChallengeAdminState','_btn','_e','_nav_row','_preview_text','show_archives','show_archive',
               'cb_archives','cb_archive_view','cb_branch_restore','cb_branch_purge_ask','cb_branch_purge_do']
        repo=NS(list_nodes=AsyncMock(return_value=[]),get_node=AsyncMock(return_value=dict(id=MID,name='قديم',status='archived')),
                list_tests=AsyncMock(return_value=[]),get_test=AsyncMock(),get_test_questions_admin=AsyncMock(return_value=[]),
                restore_branch=AsyncMock(return_value=True),delete_archived_branch=AsyncMock(return_value=True))
        return load_functions('handlers/admin/challenges.py',names,types=types,html=html,State=State,StatesGroup=StatesGroup,
            P='adm_ch:',PAGE_SIZE=8,PREVIEW_PAGE_SIZE=3,paginate=paginate,repo=repo,_render=AsyncMock(),show_home=AsyncMock(),
            KIND_LABEL={'b':('branch','فرع'),'s':('subject','مادة'),'u':('unit','وحدة')},STATUS_ICON={},ChallengeRepoError=ValueError)
    def state(self):
        return NS(clear=AsyncMock(),set_state=AsyncMock(),update_data=AsyncMock(),get_state=AsyncMock(return_value=None),get_data=AsyncMock(return_value={}))
    def call(self,action):
        return NS(data=f'adm_ch:{action}:{MID}',from_user=NS(id=42),answer=AsyncMock())
    async def test_archived_list_paginates(self):
        e=self.env();e['repo'].list_nodes.return_value=[dict(id=MID,name=str(i)) for i in range(10)]
        await e['show_archives'](None)
        e['repo'].list_nodes.assert_awaited_once_with('branch',status='archived')
        rows=e['_render'].await_args.args[2]
        self.assertEqual(len(rows),10)
        self.assertEqual(rows[-2][0].callback_data,'adm_ch:archives:1')
    async def test_archive_browses_all_statuses_and_offers_restore_purge(self):
        e=self.env();await e['show_archive'](None,'b',MID)
        e['repo'].list_nodes.assert_awaited_once_with('subject',MID,status=None)
        data=[b.callback_data for row in e['_render'].await_args.args[2] for b in row]
        self.assertIn(f'adm_ch:restore:{MID}',data);self.assertIn(f'adm_ch:purge:{MID}',data)
    async def test_unit_includes_archived_tests(self):
        e=self.env();e['repo'].get_node.return_value=dict(id=MID,name='unit',status='archived',subject_id=MID)
        await e['show_archive'](None,'u',MID)
        e['repo'].list_tests.assert_awaited_once_with(MID,include_archived=True)
    async def test_confirm_without_fsm_does_not_delete(self):
        e=self.env();c=self.call('purgeyes');await e['cb_branch_purge_do'](c,self.state())
        e['repo'].delete_archived_branch.assert_not_awaited();c.answer.assert_awaited_once()
    async def test_confirm_wrong_branch_does_not_delete(self):
        e=self.env();s=self.state();s.get_state.return_value=e['ChallengeAdminState'].confirm_branch_delete.state
        s.get_data.return_value={'branch_delete_id':'other'}
        await e['cb_branch_purge_do'](self.call('purgeyes'),s)
        e['repo'].delete_archived_branch.assert_not_awaited()
    async def test_ask_only_sets_confirmation(self):
        e=self.env();s=self.state();await e['cb_branch_purge_ask'](self.call('purge'),s)
        s.update_data.assert_awaited_once_with(branch_delete_id=MID)
        e['repo'].delete_archived_branch.assert_not_awaited()
    async def test_confirm_uses_actor_and_clears_confirmation(self):
        e=self.env();s=self.state();s.get_state.return_value=e['ChallengeAdminState'].confirm_branch_delete.state
        s.get_data.return_value={'branch_delete_id':MID}
        await e['cb_branch_purge_do'](self.call('purgeyes'),s)
        e['repo'].delete_archived_branch.assert_awaited_once_with(MID,42);s.clear.assert_awaited_once()
    async def test_restore_conflict_is_recoverable(self):
        e=self.env();e['repo'].restore_branch.side_effect=ValueError('اسم مكرر')
        await e['cb_branch_restore'](self.call('restore'),self.state())
        self.assertIn('اسم مكرر',e['_render'].await_args.args[1])
    async def test_back_navigation_cancels_confirmation(self):
        e=self.env();s=self.state();c=self.call('arc:b');c.data+=':0'
        await e['cb_archive_view'](c,s);s.clear.assert_awaited_once()


class ArchiveRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def env(self):
        from test_challenge_repo import FakeDB
        db=FakeDB()
        e=load_functions('helpers/supabase_helper/challenges.py',
            ['ChallengeRepoError','_is_unique_violation','restore_branch','delete_archived_branch'],
            supabase=db,_now=lambda:'now',log_info=lambda *a:None,logger=None)
        return e,db
    async def test_restore_only_archived_and_retains_identity(self):
        e,db=self.env();db.tables['challenge_branches']=[dict(id=MID,name='old',status='archived',archived_at='yesterday')]
        self.assertTrue(await e['restore_branch'](MID,42))
        row=db.tables['challenge_branches'][0]
        self.assertEqual(row['status'],'active');self.assertIsNone(row['archived_at'])
        self.assertEqual(row['name'],'old');self.assertFalse(await e['restore_branch'](MID,42))
    async def test_purge_is_single_rpc_not_separate_deletions(self):
        e,db=self.env();db.rpc_result=True
        self.assertTrue(await e['delete_archived_branch'](MID,42))
        self.assertEqual(db.rpc_calls,[('challenge_delete_archived_branch',{'p_branch':MID})])
    async def test_fk_error_explains_no_partial_deletion(self):
        e,db=self.env();db.rpc_error=ValueError('23503 foreign key violation')
        with self.assertRaisesRegex(e['ChallengeRepoError'],'لم يُحذف أي شيء'):
            await e['delete_archived_branch'](MID,42)
