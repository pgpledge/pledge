# syntax=docker/dockerfile:1.7
# Builds pledge against the official postgres image so the .so matches the server ABI.
#   builder  toolchain + cargo-pgrx, initialised against the system pg_config
#   test     runs `cargo pgrx test` as a non-root user (initdb refuses root)
#   runtime  postgres:$PG_MAJOR with pledge installed; also has psql + pgbench for the bench
ARG PG_MAJOR=18

FROM postgres:${PG_MAJOR} AS builder
ARG PG_MAJOR
ENV PG_MAJOR=${PG_MAJOR} \
    PG_CONFIG=/usr/lib/postgresql/${PG_MAJOR}/bin/pg_config \
    RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential clang libclang-dev libicu-dev pkg-config curl ca-certificates git \
        postgresql-server-dev-${PG_MAJOR} \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --component clippy,rustfmt \
    && chmod -R a+w $RUSTUP_HOME $CARGO_HOME
RUN cargo install cargo-pgrx --version 0.19.3 --locked
RUN cargo pgrx init --pg${PG_MAJOR} ${PG_CONFIG}
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target,id=pledge-target-${PG_MAJOR} \
    cargo pgrx package --pg-config ${PG_CONFIG} --no-default-features --features pg${PG_MAJOR} \
    && cp -r target/release/pledge-pg${PG_MAJOR} /out

FROM builder AS test
# pgrx test installs into the server's lib/extension dirs and runs initdb: needs a non-root owner.
RUN useradd -m tester \
    && chown -R tester /src /usr/local/cargo \
       /usr/lib/postgresql/${PG_MAJOR}/lib /usr/share/postgresql/${PG_MAJOR}/extension \
    && cp -r /root/.pgrx /home/tester/.pgrx && chown -R tester /home/tester/.pgrx
USER tester
ENV USER=tester
CMD cargo pgrx test pg${PG_MAJOR} --no-default-features --features "pg${PG_MAJOR}" \
    && cargo clippy --all-targets --no-default-features --features "pg${PG_MAJOR} pg_test" -- -D warnings \
    && cargo fmt --check

FROM postgres:${PG_MAJOR} AS runtime
COPY --from=builder /out/ /
