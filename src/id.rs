//! Typed binary IDs.
//!
//! On disk every id is 16 bytes of UUIDv7, laid out exactly like `uuid`, so the SQL side reuses
//! Postgres's own uuid comparison, hashing and sort support (see `sql/ids.sql`). Rust only owns
//! the text form, `<prefix>_<26 char Crockford base32>`, and id generation.

use std::cell::Cell;
use std::ffi::{CStr, CString};
use std::time::{SystemTime, UNIX_EPOCH};

use pgrx::prelude::*;
use pgrx::Uuid;

const ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// Byte -> 5-bit value, `INVALID` for anything else. Case-insensitive, O reads as 0, I and L as 1.
const INVALID: u8 = 0xff;
const DECODE: [u8; 256] = {
    let mut t = [INVALID; 256];
    let mut i = 0;
    while i < ALPHABET.len() {
        t[ALPHABET[i] as usize] = i as u8;
        t[ALPHABET[i].to_ascii_lowercase() as usize] = i as u8;
        i += 1;
    }
    t[b'O' as usize] = 0;
    t[b'o' as usize] = 0;
    t[b'I' as usize] = 1;
    t[b'i' as usize] = 1;
    t[b'L' as usize] = 1;
    t[b'l' as usize] = 1;
    t
};

pub const ACCOUNT: &str = "pla";
pub const TRANSFER: &str = "plt";
pub const ENTRY: &str = "ple";

pub fn encode(prefix: &str, bytes: &[u8; 16]) -> String {
    let mut n = u128::from_be_bytes(*bytes);
    let mut out = [0u8; 26];
    for c in out.iter_mut().rev() {
        *c = ALPHABET[(n & 31) as usize];
        n >>= 5;
    }
    // One spare byte so output()'s CString::new can append the NUL without reallocating.
    let mut s = String::with_capacity(prefix.len() + 28);
    s.push_str(prefix);
    s.push('_');
    s.extend(out.iter().map(|&c| char::from(c)));
    s
}

pub fn decode(prefix: &str, text: &str) -> Option<[u8; 16]> {
    let body = text.strip_prefix(prefix)?.strip_prefix('_')?.as_bytes();
    // 26 * 5 = 130 bits, so the first char carries only 3 (value 0..=7). Checked on the decoded
    // value, not the byte, so the O/I/L aliases are accepted there too; INVALID (0xff) fails it.
    if body.len() != 26 || DECODE[usize::from(body[0])] > 7 {
        return None;
    }
    let mut n: u128 = 0;
    for &b in body {
        let v = DECODE[usize::from(b)];
        if v == INVALID {
            return None;
        }
        n = (n << 5) | u128::from(v);
    }
    Some(n.to_be_bytes())
}

fn input(prefix: &str, type_name: &str, text: &CStr) -> Uuid {
    // Lossy only matters for the error message; valid input is ASCII and borrows.
    let s = text.to_string_lossy();
    match decode(prefix, &s) {
        Some(bytes) => Uuid::from_bytes(bytes),
        None => {
            ereport!(
                ERROR,
                PgSqlErrorCode::ERRCODE_INVALID_TEXT_REPRESENTATION,
                format!("invalid input syntax for type {type_name}: \"{s}\""),
                format!("expected {prefix}_ followed by 26 Crockford base32 characters")
            );
        }
    }
}

fn output(prefix: &str, id: Uuid) -> CString {
    // encode() only emits ASCII without NULs
    CString::new(encode(prefix, id.as_bytes()))
        .unwrap_or_else(|_| error!("pledge: id text contains a NUL byte"))
}

// The SQL for these lives in sql/ids.sql (the types must exist as shells first).
#[pg_extern(immutable, strict, parallel_safe, sql = false)]
fn account_id_in(text: &CStr) -> Uuid {
    input(ACCOUNT, "pledge.account_id", text)
}
#[pg_extern(immutable, strict, parallel_safe, sql = false)]
fn account_id_out(id: Uuid) -> CString {
    output(ACCOUNT, id)
}
#[pg_extern(immutable, strict, parallel_safe, sql = false)]
fn transfer_id_in(text: &CStr) -> Uuid {
    input(TRANSFER, "pledge.transfer_id", text)
}
#[pg_extern(immutable, strict, parallel_safe, sql = false)]
fn transfer_id_out(id: Uuid) -> CString {
    output(TRANSFER, id)
}
#[pg_extern(immutable, strict, parallel_safe, sql = false)]
fn entry_id_in(text: &CStr) -> Uuid {
    input(ENTRY, "pledge.entry_id", text)
}
#[pg_extern(immutable, strict, parallel_safe, sql = false)]
fn entry_id_out(id: Uuid) -> CString {
    output(ENTRY, id)
}

thread_local! {
    // Last (unix_ms << 12 | sub-ms) handed out by this backend; keeps ids strictly increasing.
    static LAST: Cell<u64> = const { Cell::new(0) };
}

/// UUIDv7 (RFC 9562) with 12 bits of sub-millisecond precision in rand_a, like PG18's `uuidv7()`,
/// monotonic within a backend. Implemented here so pg17 and pg18 behave the same.
pub fn uuidv7() -> [u8; 16] {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    let ms = (nanos / 1_000_000) as u64;
    let sub = ((nanos % 1_000_000) * 4096 / 1_000_000) as u64;
    let ts = LAST.with(|last| {
        let ts = ((ms << 12) | sub).max(last.get() + 1);
        last.set(ts);
        ts
    });

    let mut b = [0u8; 16];
    // SAFETY: b is a valid 16-byte buffer
    if !unsafe { pg_sys::pg_strong_random(b.as_mut_ptr().cast(), b.len()) } {
        error!("could not generate random bytes for uuidv7");
    }
    let ms = ts >> 12;
    b[..6].copy_from_slice(&ms.to_be_bytes()[2..]);
    b[6] = 0x70 | ((ts >> 8) & 0x0f) as u8;
    b[7] = ts as u8;
    b[8] = (b[8] & 0x3f) | 0x80;
    b
}

/// Fresh UUIDv7; table defaults cast it to the typed id.
#[pg_extern(volatile, parallel_safe)]
fn gen_uuidv7() -> Uuid {
    Uuid::from_bytes(uuidv7())
}

#[cfg(test)]
mod unit {
    use super::*;

    #[test]
    fn roundtrip() {
        let b = [
            0x01, 0x93, 0, 0, 0, 0, 0x70, 0, 0x80, 0, 0, 0, 0, 0, 0, 0xff,
        ];
        let s = encode(ACCOUNT, &b);
        assert_eq!(s.len(), 30);
        assert_eq!(decode(ACCOUNT, &s), Some(b));
        assert_eq!(
            decode(ACCOUNT, &format!("pla_{}", s[4..].to_lowercase())),
            Some(b)
        );
        assert_eq!(decode(TRANSFER, &s), None);
        // aliases decode in the first position too ('O' = '0')
        assert_eq!(&s[4..5], "0");
        assert_eq!(decode(ACCOUNT, &format!("pla_O{}", &s[5..])), Some(b));
        assert_eq!(decode(ACCOUNT, &format!("pla_o{}", &s[5..])), Some(b));
        assert_eq!(decode(ACCOUNT, &format!("pla_!{}", &s[5..])), None);
        assert_eq!(decode(ACCOUNT, "pla_"), None);
        assert_eq!(encode(ENTRY, &[0xff; 16]), "ple_7ZZZZZZZZZZZZZZZZZZZZZZZZZ");
        assert_eq!(decode(ENTRY, "ple_8ZZZZZZZZZZZZZZZZZZZZZZZZZ"), None);
    }
}
