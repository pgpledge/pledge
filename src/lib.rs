use pgrx::prelude::*;

::pgrx::pg_module_magic!(name, version);

mod id;
mod ledger;

// Types first (their I/O functions are declared there, not generated), schema last so it can use
// the generated functions (gen_uuidv7, _create_transfers).
extension_sql_file!("../sql/ids.sql", name = "ids", bootstrap);
extension_sql_file!("../sql/schema.sql", name = "schema", finalize);

#[cfg(any(test, feature = "pg_test"))]
#[pg_schema]
mod tests {
    use pgrx::prelude::*;

    fn one(sql: &str) -> String {
        Spi::get_one::<String>(sql).unwrap().unwrap()
    }

    fn account(name: &str, extra: &str) -> String {
        one(&format!(
            "SELECT id::text FROM pledge.create_account('{name}', 'USD'{extra})"
        ))
    }

    fn transfer(from: &str, to: &str, amount: &str) {
        Spi::run(&format!(
            "SELECT pledge.create_transfer('{from}', '{to}', {amount})"
        ))
        .unwrap();
    }

    fn balance(id: &str) -> String {
        one(&format!(
            "SELECT balance::text FROM pledge.accounts_view WHERE id::text = '{id}'"
        ))
    }

    fn verified() -> bool {
        Spi::get_one::<bool>("SELECT bool_and(ok) FROM pledge.verify()")
            .unwrap()
            .unwrap()
    }

    #[pg_test]
    fn id_text_roundtrip_and_uuid_cast() {
        let id = account("a", "");
        assert!(id.starts_with("pla_") && id.len() == 30);
        let back = one(&format!(
            "SELECT '{id}'::pledge.account_id::uuid::pledge.account_id::text"
        ));
        assert_eq!(back, id);
        let lower = one(&format!(
            "SELECT 'pla_{}'::pledge.account_id::text",
            id[4..].to_lowercase()
        ));
        assert_eq!(lower, id);
        assert_eq!(
            Spi::get_one::<i32>("SELECT pg_column_size(id) FROM pledge.accounts LIMIT 1")
                .unwrap()
                .unwrap(),
            16
        );
    }

    #[pg_test(
        error = "invalid input syntax for type pledge.account_id: \"plt_01JAAAAAAAAAAAAAAAAAAAAAAA\""
    )]
    fn id_rejects_wrong_prefix() {
        Spi::run("SELECT 'plt_01JAAAAAAAAAAAAAAAAAAAAAAA'::pledge.account_id").unwrap();
    }

    #[pg_test]
    fn id_order_is_creation_order_and_timestamp_extracts() {
        Spi::run("SELECT pledge.create_account('n' || g, 'USD') FROM generate_series(1, 50) g")
            .unwrap();
        let ordered = Spi::get_one::<bool>(
            "SELECT array_agg(name ORDER BY id) = array_agg(name ORDER BY created_at, substr(name, 2)::int)
             FROM pledge.accounts",
        )
        .unwrap()
        .unwrap();
        assert!(ordered);
        let close = Spi::get_one::<bool>(
            "SELECT bool_and(abs(extract(epoch FROM pledge.id_timestamp(id) - clock_timestamp())) < 60)
             FROM pledge.accounts",
        )
        .unwrap()
        .unwrap();
        assert!(close);
    }

    #[pg_test]
    fn transfer_moves_money_and_writes_entries() {
        let a = account("a", "");
        let b = account("b", "");
        transfer(&a, &b, "12.50");
        transfer(&b, &a, "2.50");
        assert_eq!(balance(&a), "-10.00");
        assert_eq!(balance(&b), "10.00");
        let entries = one(&format!(
            "SELECT string_agg(amount || ':' || account_previous_balance || '>' || account_current_balance
                               || '@' || account_version, ' ' ORDER BY account_version)
             FROM pledge.entries_view WHERE account_id::text = '{a}'"
        ));
        assert_eq!(entries, "-12.50:0.00>-12.50@1 2.50:-12.50>-10.00@2");
        // joins on the typed ids work without pledge on the search_path (operators in pg_catalog)
        Spi::run("SET LOCAL search_path = public").unwrap();
        let joined = Spi::get_one::<i64>(
            "SELECT count(*) FROM pledge.entries e
             JOIN pledge.transfers t ON t.id = e.transfer_id
             JOIN pledge.accounts a ON a.id = e.account_id",
        )
        .unwrap()
        .unwrap();
        assert_eq!(joined, 4);
        assert!(verified());
    }

    #[pg_test]
    fn batch_returns_transfers_in_order() {
        let a = account("a", "");
        let b = account("b", "");
        let c = account("c", "");
        let got = one(&format!(
            "SELECT string_agg(amount::text, ',') FROM pledge.create_transfers(ARRAY[
                pledge.request('{a}', '{b}', 1),
                pledge.request('{b}', '{c}', 2),
                pledge.request('{c}', '{a}', 3)])"
        ));
        assert_eq!(got, "1,2,3");
        let variadic = one(&format!(
            "SELECT count(*)::text FROM pledge.create_transfers(
                pledge.request('{a}', '{b}', 1), pledge.request('{b}', '{c}', 1))"
        ));
        assert_eq!(variadic, "2");
        assert_eq!(balance(&a), "1");
        assert!(verified());
    }

    /// Run `sql`, return its error message ("" if none). The PL/pgSQL exception block is a
    /// subtransaction, so the failed call is rolled back and the test can carry on.
    fn error_of(sql: &str) -> String {
        Spi::run(
            "CREATE OR REPLACE FUNCTION pg_temp.err(q text) RETURNS text LANGUAGE plpgsql AS $$
             BEGIN EXECUTE q; RETURN ''; EXCEPTION WHEN OTHERS THEN RETURN SQLERRM; END $$",
        )
        .unwrap();
        Spi::get_one_with_args::<String>("SELECT pg_temp.err($1)", &[sql.into()])
            .unwrap()
            .unwrap()
    }

    /// The machine-readable code clients match on (error DETAIL).
    fn code_of(sql: &str) -> String {
        Spi::run(
            "CREATE OR REPLACE FUNCTION pg_temp.code(q text) RETURNS text LANGUAGE plpgsql AS $$
             DECLARE d text; s text;
             BEGIN EXECUTE q; RETURN '';
             EXCEPTION WHEN OTHERS THEN
                 GET STACKED DIAGNOSTICS d = PG_EXCEPTION_DETAIL, s = RETURNED_SQLSTATE;
                 RETURN s || ' ' || (d::jsonb ->> 'code');
             END $$",
        )
        .unwrap();
        Spi::get_one_with_args::<String>("SELECT pg_temp.code($1)", &[sql.into()])
            .unwrap()
            .unwrap()
    }

    /// The whole error DETAIL, parsed as jsonb and printed back canonically.
    fn detail_of(sql: &str) -> String {
        Spi::run(
            "CREATE OR REPLACE FUNCTION pg_temp.detail(q text) RETURNS text LANGUAGE plpgsql AS $$
             DECLARE d text;
             BEGIN EXECUTE q; RETURN '';
             EXCEPTION WHEN OTHERS THEN
                 GET STACKED DIAGNOSTICS d = PG_EXCEPTION_DETAIL;
                 RETURN d::jsonb::text;
             END $$",
        )
        .unwrap();
        Spi::get_one_with_args::<String>("SELECT pg_temp.detail($1)", &[sql.into()])
            .unwrap()
            .unwrap()
    }

    #[pg_test]
    fn error_details_carry_the_ids_involved() {
        let a = account("a", ", allow_negative_balance => false");
        let b = account("b", "");
        let odd = one("SELECT id::text FROM pledge.create_account('odd', 'U\"S\\D')");
        let id = one("SELECT pledge.new_transfer_id()::text");
        transfer(&b, &a, "1");
        Spi::run(&format!(
            "SELECT pledge.create_transfer('{b}', '{a}', 1, id => '{id}')"
        ))
        .unwrap();
        for (sql, want) in [
            (
                format!("SELECT pledge.create_transfer('{a}', '{b}', 5)"),
                format!(r#"{{"code": "pledge.negative_balance_not_allowed", "account_id": "{a}"}}"#),
            ),
            (
                format!("SELECT pledge.create_transfer('{b}', '{a}', 2, id => '{id}')"),
                format!(r#"{{"code": "pledge.transfer_conflict", "transfer_id": "{id}"}}"#),
            ),
            (
                format!("SELECT pledge.create_transfer('{odd}', '{b}', 1)"),
                r#"{"code": "pledge.currency_mismatch", "to_currency": "USD", "from_currency": "U\"S\\D"}"#.into(),
            ),
            (
                format!("SELECT pledge.close_account('{a}')"),
                format!(r#"{{"code": "pledge.account_not_empty", "balance": "2", "account_id": "{a}"}}"#),
            ),
            (
                "DELETE FROM pledge.entries".into(),
                r#"{"code": "pledge.immutable", "table": "entries"}"#.into(),
            ),
        ] {
            assert_eq!(detail_of(&sql), want, "{sql}");
        }
    }

    #[pg_test]
    fn errors_carry_stable_codes() {
        let a = account("a", ", allow_negative_balance => false");
        let b = account("b", ", allow_positive_balance => false");
        let c = account("c", "");
        let u = account("u", "");
        let e = one("SELECT id::text FROM pledge.create_account('e', 'EUR')");
        let ghost = "pla_01JAAAAAAAAAAAAAAAAAAAAAAA";
        let id = one("SELECT pledge.new_transfer_id()::text");
        transfer(&u, &c, "1"); // c gets history, then is closed
        Spi::run(&format!(
            "SELECT pledge.create_transfer('{u}', '{c}', 1, id => '{id}')"
        ))
        .unwrap();
        transfer(&c, &u, "2"); // only an empty account can be closed
        Spi::run(&format!("SELECT pledge.close_account('{c}')")).unwrap();
        let funded = account("f", "");
        transfer(&u, &funded, "5");
        for (sql, code) in [
            (format!("SELECT pledge.close_account('{funded}')"), "account_not_empty"),
            (format!("SELECT pledge.create_transfer('{u}', '{a}', 0)"), "invalid_request"),
            (format!("SELECT pledge.create_transfer('{u}', '{u}', 1)"), "invalid_request"),
            (format!("SELECT pledge.create_transfer('{a}', '{u}', 1)"), "negative_balance_not_allowed"),
            (format!("SELECT pledge.create_transfer('{u}', '{b}', 1)"), "positive_balance_not_allowed"),
            (format!("SELECT pledge.create_transfer('{u}', '{ghost}', 1)"), "account_not_found"),
            (format!("SELECT pledge.create_transfer('{u}', '{c}', 1)"), "account_closed"),
            (format!("SELECT pledge.create_transfer('{u}', '{e}', 1)"), "currency_mismatch"),
            (format!("SELECT pledge.create_transfer('{u}', '{c}', 2, id => '{id}')"), "transfer_conflict"),
            (
                format!(
                    "SELECT pledge.create_transfers(ARRAY[pledge.request('{u}', '{a}', 1, id => '{id}'),
                                                          pledge.request('{u}', '{a}', 1, id => '{id}')])"
                ),
                "duplicate_transfer_id",
            ),
            (format!("SELECT pledge.close_account('{c}')"), "account_already_closed"),
            (format!("SELECT pledge.close_account('{ghost}')"), "account_not_found"),
            ("DELETE FROM pledge.accounts".into(), "immutable"),
            ("UPDATE pledge.accounts SET currency = 'X'".into(), "immutable"),
        ] {
            assert_eq!(code_of(&sql), format!("P0001 pledge.{code}"), "{sql}");
        }
    }

    #[pg_test]
    fn request_errors_take_precedence_over_balance_limits() {
        // a has no funds and may not go negative, so applying its leg first would report
        // negative_balance_not_allowed instead of what is actually wrong with the request.
        let a = account("a", ", allow_negative_balance => false");
        let e = one("SELECT id::text FROM pledge.create_account('e', 'EUR')");
        let c = account("c", "");
        Spi::run(&format!("SELECT pledge.close_account('{c}')")).unwrap();
        let ghost = "pla_01JAAAAAAAAAAAAAAAAAAAAAAA";
        for (sql, code) in [
            (
                format!("SELECT pledge.create_transfer('{a}', '{ghost}', 1)"),
                "account_not_found",
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{c}', 1)"),
                "account_closed",
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{e}', 1)"),
                "currency_mismatch",
            ),
        ] {
            assert_eq!(code_of(&sql), format!("P0001 pledge.{code}"), "{sql}");
        }
    }

    #[pg_test]
    fn balance_flags_are_checked_per_leg() {
        let a = account("a", ", allow_negative_balance => false");
        let b = account("b", "");
        // Leg 1 credits a, leg 2 debits it back to zero: fine because each leg is checked in order.
        let ok = error_of(&format!(
            "SELECT pledge.create_transfers(ARRAY[pledge.request('{b}', '{a}', 5),
                                                   pledge.request('{a}', '{b}', 5)])"
        ));
        assert_eq!(ok, "");
        assert_eq!(
            error_of(&format!("SELECT pledge.create_transfer('{a}', '{b}', 1)")),
            format!("Account (id={a}, name=a) does not allow negative balance")
        );
        assert_eq!(balance(&a), "0");
    }

    #[pg_test]
    fn error_messages() {
        let a = account("a", "");
        let b = account("b", "");
        let eur = one("SELECT id::text FROM pledge.create_account('e', 'EUR')");
        let pos = account("p", ", allow_positive_balance => false");
        let ghost = "pla_01JAAAAAAAAAAAAAAAAAAAAAAA";
        let cases = [
            (
                "SELECT pledge.create_transfers(NULL::pledge.transfer_request[])".to_string(),
                "Transfer requests must not be null".to_string(),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{b}', 0)"),
                "Amount (0) must be a positive finite number".into(),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{b}', -1)"),
                "Amount (-1) must be a positive finite number".into(),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{b}', 'Infinity')"),
                "Amount (Infinity) must be a positive finite number".into(),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{b}', 'NaN')"),
                "Amount (NaN) must be a positive finite number".into(),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{b}', NULL)"),
                "Amount (<NULL>) must be a positive finite number".into(),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', NULL, 1)"),
                format!("Account ids (from={a}, to=<NULL>) must not be null"),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{a}', 1)"),
                format!("Cannot transfer to the same account (id={a})"),
            ),
            (
                format!("SELECT pledge.create_transfer('{ghost}', '{a}', 1)"),
                format!("Account (id={ghost}) does not exist"),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{ghost}', 1)"),
                format!("Account (id={ghost}) does not exist"),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{pos}', 1)"),
                format!("Account (id={pos}, name=p) does not allow positive balance"),
            ),
            (
                format!("SELECT pledge.create_transfer('{a}', '{eur}', 1)"),
                "Cannot transfer between different currencies (USD and EUR)".into(),
            ),
        ];
        for (sql, want) in cases {
            assert_eq!(error_of(&sql), want, "{sql}");
        }
        assert!(verified());
    }

    #[pg_test]
    fn ledger_rows_cannot_be_removed_or_rewritten() {
        let a = account("a", "");
        let b = account("b", "");
        transfer(&a, &b, "1");
        for (sql, want) in [
            ("DELETE FROM pledge.entries", "pledge.entries: rows cannot be removed (DELETE rejected)"),
            (
                "TRUNCATE pledge.transfers CASCADE",
                "pledge.transfers: rows cannot be removed (TRUNCATE rejected)",
            ),
            ("DELETE FROM pledge.accounts", "pledge.accounts: rows cannot be removed (DELETE rejected)"),
            (
                "UPDATE pledge.accounts SET currency = 'EUR'",
                "pledge.accounts: id, currency, created_at cannot be changed",
            ),
            (
                "UPDATE pledge.transfers SET amount = 2",
                "pledge.transfers: id, from_account_id, to_account_id, amount, created_at cannot be changed",
            ),
            (
                "UPDATE pledge.entries SET account_current_balance = 0",
                "pledge.entries: id, account_id, transfer_id, amount, account_current_balance, account_version cannot be changed",
            ),
            // allowed: descriptive fields
            ("UPDATE pledge.accounts SET name = 'renamed'", ""),
            ("UPDATE pledge.transfers SET metadata = '{\"note\": 1}'", ""),
        ] {
            assert_eq!(error_of(sql), want, "{sql}");
        }
        assert!(verified());
    }

    #[pg_test]
    fn internal_entry_point_rejects_ragged_arrays() {
        assert_eq!(
            error_of(
                "SELECT pledge._create_transfers(false, ARRAY[NULL::uuid], ARRAY[gen_random_uuid()],
                                                 '{}'::uuid[], ARRAY[1::numeric], ARRAY[NULL::timestamptz],
                                                 ARRAY[NULL::text])"
            ),
            "pledge._create_transfers: all request arrays must have equal length"
        );
    }

    #[pg_test]
    fn supplied_transfer_id_makes_retries_idempotent() {
        let a = account("a", "");
        let b = account("b", "");
        let c = account("c", "");
        let id = one("SELECT pledge.new_transfer_id()::text");
        let call = |amount: &str, meta: &str| {
            one(&format!(
                "SELECT string_agg(id::text, ',') FROM pledge.create_transfer('{a}', '{b}', {amount},
                    metadata => {meta}, id => '{id}')"
            ))
        };
        assert_eq!(call("1.0", "'{\"x\": 1}'"), id);
        assert_eq!(
            call("1.00", "'{\"x\": 1}'"),
            id,
            "numerically equal amount replays"
        );
        assert_eq!(balance(&b), "1.0", "the retry moved no money");

        let different = format!("Transfer (id={id}) already exists with different values");
        assert_eq!(error_of(&format!("SELECT pledge.create_transfer('{a}', '{b}', 2, metadata => '{{\"x\": 1}}', id => '{id}')")), different);
        assert_eq!(
            error_of(&format!(
                "SELECT pledge.create_transfer('{a}', '{b}', 1, id => '{id}')"
            )),
            different,
            "different metadata"
        );
        assert_eq!(error_of(&format!("SELECT pledge.create_transfer('{a}', '{c}', 1, metadata => '{{\"x\": 1}}', id => '{id}')")), different, "different account");

        // A batch mixing a retry with a new transfer: only the new one moves money, rows come
        // back in request order.
        let got = one(&format!(
            "SELECT string_agg(amount::text, ',') FROM pledge.create_transfers(ARRAY[
                pledge.request('{b}', '{c}', 7), pledge.request('{a}', '{b}', 1.0, id => '{id}')],
                metadata => '{{\"x\": 1}}')"
        ));
        assert_eq!(got, "7,1.0");
        assert_eq!(balance(&b), "-6.0");
        assert_eq!(
            error_of(&format!(
                "SELECT pledge.create_transfers(ARRAY[pledge.request('{a}', '{b}', 1, id => '{id}'),
                                                      pledge.request('{a}', '{b}', 1, id => '{id}')])"
            )),
            format!("Transfer id ({id}) appears more than once in the request")
        );
        assert!(verified());
    }

    #[pg_test]
    fn batch_items_carry_their_own_event_at_and_metadata() {
        let a = account("a", "");
        let b = account("b", "");
        let id = one("SELECT pledge.new_transfer_id()::text");
        let sql = format!(
            "SELECT string_agg(coalesce(metadata->>'order', '-') || '@' || event_at::date, ',' ORDER BY amount)
             FROM pledge.create_transfers(ARRAY[
                 pledge.request('{a}', '{b}', 1, metadata => '{{\"order\": 1}}', event_at => '2024-01-01'),
                 pledge.request('{a}', '{b}', 2, id => '{id}', metadata => '{{\"order\": 2.0}}'),
                 pledge.request('{a}', '{b}', 3)],
                 event_at => '2025-06-01', metadata => '{{\"order\": \"default\"}}')"
        );
        assert_eq!(one(&sql), "1@2024-01-01,2.0@2025-06-01,default@2025-06-01");
        // a retry compares each transfer's own metadata, as jsonb (2.0 = 2.00)
        let retry = |meta: &str| {
            format!(
                "SELECT count(*)::text FROM pledge.create_transfers(ARRAY[
                     pledge.request('{a}', '{b}', 2, id => '{id}', metadata => '{meta}')],
                     event_at => '2025-06-01')"
            )
        };
        assert_eq!(one(&retry("{\"order\": 2.00}")), "1");
        assert_eq!(
            error_of(&retry("{\"order\": 3}")),
            format!("Transfer (id={id}) already exists with different values")
        );
        assert_eq!(balance(&b), "6");
        assert!(verified());
    }

    #[pg_test]
    fn write_path_uses_generic_plans() {
        let config = one("SELECT array_to_string(proconfig, ',') FROM pg_proc
             WHERE oid = 'pledge._create_transfers'::regproc");
        assert_eq!(config, "plan_cache_mode=force_generic_plan");
    }

    #[pg_test]
    fn batches_are_all_or_nothing() {
        let a = account("a", "");
        let b = account("b", ", allow_negative_balance => false");
        assert_eq!(
            one("SELECT count(*)::text FROM pledge.create_transfers('{}'::pledge.transfer_request[])"),
            "0"
        );
        // leg 1 is fine on its own; leg 2 overdraws b, so neither may persist
        assert_eq!(
            error_of(&format!(
                "SELECT pledge.create_transfers(ARRAY[pledge.request('{a}', '{b}', 5),
                                                      pledge.request('{b}', '{a}', 6)])"
            )),
            format!("Account (id={b}, name=b) does not allow negative balance")
        );
        assert_eq!(balance(&a), "0");
        assert_eq!(one("SELECT count(*)::text FROM pledge.transfers"), "0");
        assert!(verified());
    }

    #[pg_test]
    fn closed_accounts_take_no_transfers() {
        let a = account("a", "");
        let b = account("b", "");
        transfer(&a, &b, "3");
        assert_eq!(
            error_of(&format!("SELECT pledge.close_account('{b}')")),
            format!("Account (id={b}, name=b) has a non-zero balance (3)")
        );
        transfer(&b, &a, "3");
        let closed = one(&format!(
            "SELECT (closed_at IS NOT NULL)::text FROM pledge.close_account('{b}')"
        ));
        assert_eq!(closed, "true");
        for (from, to) in [(&a, &b), (&b, &a)] {
            assert_eq!(
                error_of(&format!(
                    "SELECT pledge.create_transfer('{from}', '{to}', 1)"
                )),
                format!("Account (id={b}, name=b) is closed")
            );
        }
        assert_eq!(
            error_of(&format!("SELECT pledge.close_account('{b}')")),
            format!("Account (id={b}) is already closed")
        );
        assert_eq!(balance(&b), "0", "history stays");
        assert_eq!(
            one(&format!(
                "SELECT version::text FROM pledge.accounts WHERE id::text = '{b}'"
            )),
            "2"
        );
        assert!(verified());
    }
}

/// This module is required by `cargo pgrx test` invocations.
/// It must be visible at the root of your extension crate.
#[cfg(test)]
pub mod pg_test {
    pub fn setup(_options: Vec<&str>) {}

    #[must_use]
    pub fn postgresql_conf_options() -> Vec<&'static str> {
        vec![]
    }
}
