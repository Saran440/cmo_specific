# -*- coding: utf-8 -*-
from openerp import models, fields, api, tools, _
from openerp.exceptions import Warning as UserError, ValidationError


class CMOActionAssetCompute(models.TransientModel):
    """ CMO Action for Compute Asset Depreciation """
    _name = 'cmo.action.asset.compute'
    _inherit = 'pabi.action'

    # Criteria
    calendar_period_id = fields.Many2one(
        'account.period.calendar',
        string='Period',
        domain=[('state', '=', 'draft')],
        default=lambda self: self.env['account.period'].find().id,
        required=True,
    )
    compute_method = fields.Selection(
        [('standard', 'Standard - 1 JE per Asset (more JEs)'),
         ('grouping', 'Grouping - 1 JE per Asset Profile'), ],
        string='Compute Method',
        default='standard',
        required=True,
        help="Method of generating depreciation journal entries\n"
        "* Standard: create 1 JE for each asset depreciation line\n"
        "* Grouping: create 1 JE by grouping depreciation with same "
        "asset profile",
    )
    grouping_date = fields.Date(
        string='Date',
        default=lambda self: fields.Date.context_today(self),
        help="For Grouping method, this date will be used to create JE",
    )
    profile_ids = fields.Many2many(
        'account.asset.profile',
        'cmo_action_asset_compute_account_asset_profile_rel',
        'compute_id', 'profile_id',
        string='Profile',
        # domain=[('no_depreciation', '=', False)],
    )
    test_log_ids = fields.One2many(
        'cmo.action.asset.compute.test.log',
        'wizard_id',
        string='Asset Test Log',
        readonly=True,
    )
    run_state = fields.Selection(
        [('run', 'Run Asset'),
         ('test', 'Test Result')],
        string='Run State',
        default='run',
    )
    batch_note = fields.Char(
        string='Batch Note',
        help="Note that will be filled in asset depreciation batch",
    )

    @api.onchange('calendar_period_id')
    def _onchange_calendar_period_id(self):
        self.profile_ids = False
        self.test_assets = '[]'

    @api.onchange('profile_ids')
    def _onchange_profile_ids(self):
        self.test_assets = '[]'

    @api.multi
    def check_affected_asset(self):
        self.ensure_one()
        asset_ids = self._search_asset(self.calendar_period_id,
                                       self.profile_ids.ids)
        # Assume 1 depre line for 1 asset always
        raise UserError(
            _('%s assets will be computed in this operation') % len(asset_ids))

    @api.model
    def _search_asset(self, period, profile_ids,
                      return_as_groups=False):
        # SQL to find asset_ids based on search criteria
        from_sql = """
            from account_asset a
            join account_asset_profile p on a.profile_id = p.id
            where 1 = 1
            %s -- profile_cond
        """
        p = ''
        if profile_ids:
            profiles = str(profile_ids).replace('[', '(').replace(']', ')')
            p = 'and p.id in %s' % profiles
        from_str = from_sql % (p)
        # With group by
        groups = []
        if return_as_groups:
            # Compute method grouping, find the group by criterias
            # By doing this, 1 JE will be created for each asset profile
            GROUPBY = ['profile_id']
            # GROUPBY = ['account_depreciation_id',
            #            'account_expense_depreciation_id',
            #            'owner_section_id',
            #            'owner_project_id',
            #            'owner_invest_asset_id',
            #            'owner_invest_construction_phase_id', ]
            fields_str = ', '.join(GROUPBY)
            self._cr.execute("select distinct %s %s" %
                             (fields_str, from_str))
            groups = self._cr.dictfetchall()  # i.e., [{'x': 1, 'y': 2}, {}]
        else:
            # Compute method standard, 1 group only
            groups = [{1: 1}]  # and 1=1, which is no harm

        group_assets = {}  # Return as {'Group1': [1,2,3,4], ...}
        for g in groups:
            # Formulate where clause
            wheres = []
            for key, val in g.iteritems():
                if val:
                    wheres.append('%s = %s' % (key, val))
                else:
                    wheres.append('%s is null' % key)
            where_str = ' and '.join(wheres)
            # Select assets based on search criteria
            self._cr.execute("select distinct a.id %s and %s" %
                             (from_str, where_str))
            asset_ids = [x[0] for x in self._cr.fetchall()] or [0]
            # Find valid depreciation line, based on asset_ids
            self._cr.execute("""
                select a.id
                from account_asset_line al
                    join account_asset a on a.id = al.asset_id
                where a.state = 'open' and a.type = 'normal'
                    and a.active = true and al.type = 'depreciate'
                    and al.init_entry = false and al.move_check = false
                    and exists (select 1 from account_asset where a.id in %s)
                    and al.line_date between %s and %s
            """, (tuple(asset_ids), period.date_start, period.date_stop))
            asset_ids = [x[0] for x in self._cr.fetchall()]
            if asset_ids:
                group_assets.update({str(g): asset_ids})

        if return_as_groups:  # {group: asset_ids, ...}
            return group_assets or {}
        else:  # asset_ids
            return group_assets and group_assets.values()[0] or []

    @api.multi
    def asset_compute(self, period_id, profile_ids,
                      batch_note, compute_method='standard',
                      grouping_date=False):
        period = self.env['account.period'].browse(period_id)
        # Block future period
        current_period = self.env['account.period'].find()
        if not self._context.get('allow_future_period', False) and \
                period.date_start > current_period.date_start:
            raise ValidationError(_('Compute asset depreciation for '
                                    'future period is not allowed!'))
        # Batch ID
        depre_batch = self.env['cmo.asset.depre.batch'].new_batch(period,
                                                                  batch_note)
        group_assets = {}  # Group of assets from search
        created_move_ids = []
        error_logs = []
        if compute_method == 'grouping':  # Groupby Account, Depre Budget
            group_assets = self._search_asset(period, profile_ids,
                                              return_as_groups=True)
        else:  # standard
            asset_ids = self._search_asset(period, profile_ids)
            if asset_ids:
                group_assets['N/A'] = asset_ids
        if not group_assets:
            raise ValidationError(
                _('No more assets to compute for this period!'))
        # Compute for each group (1 group for standard case)
        for grp_name, asset_ids in group_assets.iteritems():
            assets = self.env['account.asset'].browse(asset_ids)
            merge_move = (compute_method == 'grouping') and True or False
            move_ids, error_log = assets._compute_entries(
                period, check_triggers=True,
                merge_move=merge_move, merge_date=grouping_date)
            moves = self.env['account.move'].browse(move_ids)
            moves.write({'asset_depre_batch_id': depre_batch.id})
            created_move_ids += move_ids
            if error_log and error_log != '':
                error_logs.append(error_log)
        # Return
        result_msg = '\n'.join(error_logs)
        if not result_msg:
            result_msg = _('Computed depreciation created %s '
                           'journal entries') % len(created_move_ids)
        return (depre_batch, result_msg)

    @api.multi
    def pabi_action(self):
        self.ensure_one()
        # Prepare job information
        process_xml_id = 'cmo_async_process.asset_compute'
        job_desc = _('Compute Asset Depreciation for %s by %s' %
                     (self.calendar_period_id.display_name,
                      self.env.user.display_name))
        func_name = 'asset_compute'
        # Prepare kwargs, the params for method action_generate
        kwargs = {'period_id': self.calendar_period_id.id,
                  'profile_ids': self.profile_ids.ids,
                  'batch_note': self.batch_note,
                  'compute_method': self.compute_method,
                  'grouping_date': self.grouping_date,
                  }
        # Call the function
        res = super(CMOActionAssetCompute, self).\
            pabi_action(process_xml_id, job_desc, func_name, **kwargs)
        return res

    @api.multi
    def action_back(self):
        self.ensure_one()
        self.write({'run_state': 'run'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'cmo.action.asset.compute',
            'view_mode': 'form',
            'view_type': 'form',
            'res_id': self.id,
            'views': [(False, 'form')],
            'target': 'new',
        }

    @api.multi
    def run_asset_test(self):
        """ Based on matched assets, run through series of test """
        self.ensure_one()
        self.test_log_ids.unlink()
        asset_ids = self._search_asset(
            self.calendar_period_id, self.profile_ids.ids
        )
        # TEST
        assets = self.env['account.asset'].browse(asset_ids)
        self._log_test_period_closed(self.calendar_period_id)
        self._log_test_asset_profile_account(assets)
        self._log_test_prev_depre_posted(self.calendar_period_id, assets)
        self.write({'run_state': 'test'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'cmo.action.asset.compute',
            'view_mode': 'form',
            'view_type': 'form',
            'res_id': self.id,
            'views': [(False, 'form')],
            'target': 'new',
        }

    @api.model
    def _log_test_period_closed(self, period):
        if period.state == 'done':
            message = _('Period %s is closed') % period.calendar_name
            self.write({'test_log_ids': [(0, 0, {'message': message})]})
        return True

    @api.model
    def _log_test_asset_profile_account(self, assets):
        """ Test whether any of account from asset profile is inactive """
        if assets:
            logs = []
            self._cr.execute("""
                select id as account_id from account_account
                where active = false
                and id in (
                select distinct account_depreciation_id account_id
                from account_asset_profile
                where account_depreciation_id is not null and id in
                (select distinct profile_id from account_asset where id in %s)
                union
                select distinct account_expense_depreciation_id account_id
                from account_asset_profile
                where account_expense_depreciation_id is not null and id in
                (select distinct profile_id from account_asset where id in %s))
            """, (tuple(assets.ids), tuple(assets.ids)))
            account_ids = [x[0] for x in self._cr.fetchall()]
            accounts = self.env['account.account'].browse(account_ids)
            for account in accounts:
                message = _("Inactive asset profile's account code: %s") % \
                    account.code
                logs.append((0, 0, {'message': message}))
            self.write({'test_log_ids': logs})
        return True

    @api.multi
    def _log_test_prev_depre_posted(self, period, assets):
        self.ensure_one()
        Asset = self.env['account.asset']
        if assets:
            logs = []
            unposted_asset_ids = assets._test_prev_depre_unposted(period)
            assets = Asset.browse(unposted_asset_ids)
            for asset in assets:
                message = \
                    _("Unposted lines prior to period: %s")
                logs.append((0, 0, {'asset_id': asset.id,
                                    'message': message % period.name}))
            self.write({'test_log_ids': logs})
        return True


class CMOActionAssetComputeTestLog(models.TransientModel):
    _name = 'cmo.action.asset.compute.test.log'
    _order = 'id desc'

    wizard_id = fields.Many2one(
        'cmo.action.asset.compute',
        string='Wizard ID',
        ondelete='cascade',
        readonly=True,
    )
    asset_id = fields.Many2one(
        'account.asset',
        string='Asset',
        readonly=True,
    )
    message = fields.Char(
        string='Message',
        readonly=True,
    )


class SummaryJournalView(models.Model):
    _name = 'summary.journal.view'
    _order = 'operating_unit_id, account_id desc'
    _auto = False

    id = fields.Integer(
        string='ID',
        readonly=True,
    )
    asset_depre_batch_id = fields.Many2one(
        'cmo.asset.depre.batch',
        string='Asset Depreciation Batch ID',
        readonly=True,
        help="To group move line on same computation",
    )
    account_id = fields.Many2one(
        'account.account',
        string='Account',
        readonly=True,
    )
    operating_unit_id = fields.Many2one(
        'operating.unit',
        string='Operating Unit',
        readonly=True,
    )
    analytic_account_id = fields.Many2one(
        'account.analytic.account',
        string='Project',
        readonly=True,
    )
    debit = fields.Float(
        string='debit',
        readonly=True,
    )
    credit = fields.Float(
        string='credit',
        readonly=True,
    )

    def _get_sql_view(self):
        sql_view = """
            SELECT ROW_NUMBER() OVER(ORDER BY asset_depre_batch_id) AS id,
                   account_id,
                   operating_unit_id,
                   analytic_account_id,
                   asset_depre_batch_id,
                   sum(debit) AS debit, sum(credit) AS credit
            FROM account_move_line
            GROUP BY operating_unit_id,
                     analytic_account_id,
                     asset_depre_batch_id,
                     account_id
        """
        return sql_view

    def init(self, cr):
        tools.drop_view_if_exists(cr, self._table)
        cr.execute("""CREATE OR REPLACE VIEW %s AS (%s)"""
                   % (self._table, self._get_sql_view()))


class CMOAssetDepreBatch(models.Model):
    _name = 'cmo.asset.depre.batch'
    _order = 'id desc'
    _description = 'Asset Depreciation Compute Batch'

    name = fields.Char(
        string='Name',
        compute='_compute_name',
        store=True,
        help="As <period>-<run number>",
    )
    run_number = fields.Integer(
        string='Run Number',
        readonly=True,
        required=True,
    )
    period_id = fields.Many2one(
        'account.period',
        string='Period',
        readonly=True,
        required=True,
    )
    note = fields.Char(
        string='Note',
        readonly=True,
    )
    state = fields.Selection(
        [('draft', 'Draft'),
         ('posted', 'Posted'),
         ('cancel', 'Cancelled')],
        string='State',
        default='draft',
        help="* Draft: first created, user prevew\n"
        "* Posted: all journal entries posted\n"
        "* Cancelled: user choose to delete and will redo again"
    )
    move_ids = fields.One2many(
        'account.move',
        'asset_depre_batch_id',
        string='Journal Entries',
    )
    move_line_ids = fields.One2many(
        'account.move.line',
        'asset_depre_batch_id',
        string='Journal Items',
    )
    summary_ids = fields.One2many(
        'summary.journal.view',
        'asset_depre_batch_id',
        string='Summary',
    )
    amount = fields.Float(
        string='Depreciation Amount',
        compute='_compute_amount',
    )

    move_count = fields.Integer(
        string='JE Count',
        compute='_compute_moves',
    )
    journal_number = fields.Text(
        string='Journal Number',
        compute='_compute_je_number',
    )

    @api.multi
    def _compute_je_number(self):
        for rec in self:
            account_moves = rec.move_ids.sorted(lambda l: l.name)
            if account_moves:
                rec.journal_number = '%s - %s' % (account_moves[0].name,
                                                  account_moves[-1].name)
        return True

    @api.multi
    def _compute_moves(self):
        for rec in self:
            rec.move_count = len(rec.move_ids)
        return True

    @api.multi
    def open_entries(self):
        self.ensure_one()
        return {
            'name': _("Journal Entries"),
            'view_type': 'form',
            'view_mode': 'tree,form',
            'res_model': 'account.move',
            'view_id': False,
            'type': 'ir.actions.act_window',
            'context': self._context,
            'nodestroy': True,
            'domain': [('id', 'in', self.move_ids.ids)],
        }

    @api.model
    def new_batch(self, period, note):
        # Get last batch's run_number
        batch = self.search([('period_id', '=', period.id)],
                            order='run_number desc', limit=1)
        next_run = batch and (batch.run_number + 1) or 1
        new_batch = self.create({'period_id': period.id,
                                 'run_number': next_run,
                                 'note': note, })
        return new_batch

    @api.multi
    @api.depends('run_number', 'period_id')
    def _compute_name(self):
        for rec in self:
            number = str(rec.run_number)
            rec.name = '%s-%s' % (rec.period_id.name, number.zfill(2))
        return True

    @api.multi
    def delete_unposted_entries(self):
        """ For fast removal, we use SQL """
        for rec in self:
            # disable trigger, for faster execution
            self._cr.execute("""
                alter table account_move_line disable trigger all""")
            self._cr.execute("""
                alter table account_move disable trigger all""")
            # reset move_check in depre line
            self._cr.execute("""
                update account_asset_line
                set move_check = false, move_id = null
                where move_id in (
                    select id from account_move
                    where asset_depre_batch_id = %s)
            """, (rec.id, ))
            # delete from table
            self._cr.execute("""
                delete from account_move_line where asset_depre_batch_id = %s
            """, (rec.id, ))
            self._cr.execute("""
                delete from account_move where asset_depre_batch_id = %s
                and state = 'draft' and name in (null, '/')
            """, (rec.id, ))
            # enable trigger, for faster execution
            self._cr.execute("""
                alter table account_move_line enable trigger all""")
            self._cr.execute("""
                alter table account_move enable trigger all""")
        self.write({'state': 'cancel'})
        return True

    @api.multi
    def post_entries(self):
        """ For fast post, just by pass all other checks """
        for batch in self:
            ctx = {'fiscalyear_id': batch.period_id.fiscalyear_id.id}
            Sequence = self.with_context(ctx).env['ir.sequence']
            self._cr.execute("""
                select m.id, j.sequence_id, m.ref,
                    (select max(asset_id)
                     from account_move_line where move_id = m.id) asset_id
                from account_move m
                join account_journal j on j.id = m.journal_id
                where m.asset_depre_batch_id = %s
                and m.state = 'draft' and m.name in (null, '/')
            """, (batch.id, ))
            moves = [(x[0], x[1], x[2], x[3]) for x in self._cr.fetchall()]
            # Prepare sequence for immediate update
            for move_id, sequence_id, ref, asset_id in moves:
                new_name = Sequence.next_by_id(sequence_id)
                # Update sequence
                self._cr.execute("""
                    update account_move
                    set name = %s, state = 'posted' where id = %s
                """, (new_name, move_id))
                # Update depreciation line
                self._cr.execute("""
                    update account_asset_line asl
                    set move_id = %s, move_check = true
                    where asl.name = %s
                """, (move_id, ref))  # ref is the depre_line_id
                # Finally recompute asset residual value
                asset = self.env['account.asset'].browse(asset_id)
                asset._compute_depreciation()
                asset._set_close_asset_zero_value()
                self._cr.commit()
        self.write({'state': 'posted'})
        return True

    # @api.multi
    # def post_entries(self):
    #     for rec in self:
    #         rec.move_ids.post()
    #         rec.write({'state': 'posted'})
    #     return True

    @api.multi
    def _compute_amount(self):
        self._cr.execute("""
            select asset_depre_batch_id, sum(debit) as amount
            from account_move_line
            where asset_depre_batch_id in %s
            group by asset_depre_batch_id
        """, (tuple(self.ids), ))
        amount_dict = dict([(x[0], x[1]) for x in self._cr.fetchall()])
        for rec in self:
            rec.amount = amount_dict.get(rec.id, 0.0)
        return True
