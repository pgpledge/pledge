-- prints: transfers|check
SELECT
    (
        SELECT
            count(*)
        FROM
            pledge.transfers) AS transfers,
    CASE WHEN NOT EXISTS (
        SELECT
            1
        FROM
            pledge.entries
        GROUP BY
            transfer_id
        HAVING
            sum(amount) <> 0
            OR count(*) <> 2)
        AND NOT EXISTS (
            SELECT
                1
            FROM
                pledge.accounts a
            LEFT JOIN (
                SELECT
                    account_id,
                    sum(amount) s,
                    max(account_version) v,
                    count(*) n
                FROM
                    pledge.entries
                GROUP BY
                    account_id) e ON e.account_id = a.id
            WHERE
                a.balance <> coalesce(e.s, 0)
                OR a.version <> coalesce(e.v, 0)
                OR a.version <> coalesce(e.n, 0)) THEN
        'ok'
    ELSE
        'FAIL'
    END AS check;

