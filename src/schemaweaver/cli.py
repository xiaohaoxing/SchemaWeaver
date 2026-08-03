"""CLI for SchemaWeaver."""
import click
from pathlib import Path
from rich.console import Console
from .pipeline import ExtractionPipeline
from .adapters.base import load_adapters
from .store import SchemaStore
from .live_sync import connect_mysql, introspect_mysql, mysql_params_from_environment
from . import __version__


console = Console()


@click.group()
@click.version_option(__version__)
def main():
    """SchemaWeaver: Static ORM-annotation → DB schema + business-semantics MCP server."""
    pass


@main.command()
@click.argument("repo_root", type=click.Path(exists=True))
@click.option("--alias", required=True, help="Repo alias (e.g., cfs, cfs-jiayou)")
@click.option("--orm", help="Force adapter id (default: auto-detect)")
@click.option("--domain-map", type=click.Path(exists=True), help="JSON: table prefix → domain")
@click.option("--db", default="~/.schemaweaver/schemaweaver.db", help="SQLite DB path")
def extract(repo_root, alias, orm, domain_map, db):
    """Extract schema from source code (like gitnexus analyze)."""
    repo_root = Path(repo_root).resolve()
    db_path = Path(db).expanduser().resolve()

    # Load domain map if provided
    dm = None
    if domain_map:
        import json
        dm = json.loads(Path(domain_map).read_text())

    # Load adapters
    registry = load_adapters()
    if not registry.all():
        console.print("[red]Error: No adapters registered. Check entry_points.[/red]")
        return

    # Filter by ORM if specified
    if orm:
        filtered = [a for a in registry.all() if a.id == orm]
        if not filtered:
            console.print(f"[red]Error: Adapter '{orm}' not found. Available: {[a.id for a in registry.all()]}[/red]")
            return
        # Create new registry with filtered adapter
        from .adapters.base import AdapterRegistry
        new_reg = AdapterRegistry()
        for a in filtered:
            new_reg.register(a)
        registry = new_reg

    console.print(f"[bold]Extracting schema from {repo_root}[/bold]")
    console.print(f"Repo alias: {alias}")
    console.print(f"Adapters: {[a.id for a in registry.all()]}")
    console.print(f"DB: {db_path}")
    console.print()

    # Run pipeline
    pipeline = ExtractionPipeline(
        repo_root=repo_root,
        registry=registry,
        db_path=db_path,
        repo_alias=alias,
        extractor_ver=__version__,
        domain_map=dm,
    )
    report = pipeline.run()

    console.print("[green]✓ Extraction complete[/green]")
    console.print(f"  Tables: {report.tables}")
    console.print(f"  Enums: {report.enums}")
    console.print(f"  Relations: {report.relations}")
    console.print(f"  i18n keys: {report.i18n_keys}")
    if report.warnings:
        console.print(f"[yellow]Warnings ({len(report.warnings)}):[/yellow]")
        for w in report.warnings[:5]:
            console.print(f"  - {w}")
        if len(report.warnings) > 5:
            console.print(f"  ... and {len(report.warnings) - 5} more")


@main.command(name="sync-live")
@click.option("--env", type=click.Choice(["test", "prod"]), required=True)
@click.option("--source-repo", type=click.Path(exists=True, path_type=Path), required=True,
              help="Source repository used to preserve ORM business semantics")
@click.option("--alias", required=True, help="Separate local alias for this environment")
@click.option("--db", default="~/.schemaweaver/schemaweaver.db", help="SQLite DB path")
@click.option("--host", help="Override MYSQL_<ENV>_HOST")
@click.option("--port", type=int, help="Override MYSQL_<ENV>_PORT")
@click.option("--user", help="Override MYSQL_<ENV>_USER")
@click.option("--password", help="Override MYSQL_<ENV>_PASSWORD")
@click.option("--database", help="Override MYSQL_<ENV>_DB")
def sync_live(env, source_repo, alias, db, host, port, user, password, database):
    """Refresh an index from a live MySQL database."""
    db_path = Path(db).expanduser().resolve()
    params = mysql_params_from_environment(env)
    params.update({
        key: value for key, value in {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
        }.items() if value is not None
    })

    registry = load_adapters()
    if not registry.all():
        raise click.ClickException("No adapters registered. Check entry_points.")
    console.print(f"[bold]Extracting source semantics from {source_repo}[/bold]")
    report = ExtractionPipeline(
        repo_root=source_repo.resolve(),
        registry=registry,
        db_path=db_path,
        repo_alias=alias,
        extractor_ver=__version__,
    ).run()
    console.print(f"Source semantics: {report.tables} tables, {report.enums} enums")

    connection = None
    try:
        console.print(f"[bold]Reading live schema ({env}) from {params['host']}:{params['port']}/{params['database']}[/bold]")
        connection = connect_mysql(**params)
        live_tables = introspect_mysql(connection, params["database"])
    except Exception as exc:
        raise click.ClickException(f"Live schema sync failed ({env}): {exc}") from exc
    finally:
        if connection is not None:
            connection.close()

    store = SchemaStore(db_path)
    try:
        store.sync_live_schema(alias, live_tables, env)
    finally:
        store.close()
    console.print(f"[green]✓ Live schema sync complete[/green] {alias}: {len(live_tables)} tables")


@main.command()
@click.option("--db", default="~/.schemaweaver/schemaweaver.db", help="SQLite DB path")
def mcp(db):
    """Start MCP server (stdio)."""
    from .mcp_server import build_server
    server = build_server(db)
    server.run(transport="stdio")


@main.command(name="list")
@click.option("--db", default="~/.schemaweaver/schemaweaver.db", help="SQLite DB path")
def list_repos(db):
    """List indexed repos."""
    db_path = Path(db).expanduser()
    if not db_path.exists():
        console.print("[yellow]No repos indexed yet. Run 'schemaweaver extract' first.[/yellow]")
        return

    store = SchemaStore(db_path)
    try:
        rows = store.conn.execute("""
            SELECT repo_id, root_path, orm_primary, extracted_at, table_count, enum_count
            FROM repos ORDER BY extracted_at DESC
        """).fetchall()
        if not rows:
            console.print("[yellow]No repos indexed yet.[/yellow]")
            return
        console.print("[bold]Indexed repos:[/bold]")
        for row in rows:
            repo_id, root_path, orm, extracted_at, table_count, enum_count = row
            console.print(f"  {repo_id}: {table_count} tables, {enum_count} enums (extracted {extracted_at[:10]})")
    finally:
        store.close()


@main.command()
@click.argument("alias")
@click.option("--db", default="~/.schemaweaver/schemaweaver.db", help="SQLite DB path")
def status(alias, db):
    """Show status of an indexed repo."""
    db_path = Path(db).expanduser()
    if not db_path.exists():
        console.print(f"[red]Repo '{alias}' not indexed.[/red]")
        return

    store = SchemaStore(db_path)
    try:
        row = store.conn.execute("""
            SELECT repo_id, root_path, orm_primary, extracted_at, table_count, enum_count
            FROM repos WHERE repo_id = ?
        """, (alias,)).fetchone()
        if not row:
            console.print(f"[red]Repo '{alias}' not indexed.[/red]")
            return
        repo_id, root_path, orm, extracted_at, table_count, enum_count = row
        console.print(f"[bold]{repo_id}[/bold]")
        console.print(f"  Root: {root_path}")
        console.print(f"  ORM: {orm}")
        console.print(f"  Extracted: {extracted_at}")
        console.print(f"  Tables: {table_count}")
        console.print(f"  Enums: {enum_count}")
    finally:
        store.close()


@main.command()
@click.argument("alias")
@click.option("--db", default="~/.schemaweaver/schemaweaver.db", help="SQLite DB path")
@click.option("--force", is_flag=True, help="Skip confirmation")
def remove(alias, db, force):
    """Remove an indexed repo."""
    db_path = Path(db).expanduser()
    if not db_path.exists():
        console.print(f"[red]Repo '{alias}' not indexed.[/red]")
        return

    store = SchemaStore(db_path)
    try:
        row = store.conn.execute("SELECT repo_id FROM repos WHERE repo_id = ?", (alias,)).fetchone()
        if not row:
            console.print(f"[red]Repo '{alias}' not indexed.[/red]")
            return

        if not force:
            if not click.confirm(f"Remove repo '{alias}' and all its data?"):
                console.print("Cancelled.")
                return

        store.conn.execute("DELETE FROM repos WHERE repo_id = ?", (alias,))
        store.conn.commit()
        console.print(f"[green]✓ Removed repo '{alias}'[/green]")
    finally:
        store.close()


if __name__ == "__main__":
    main()
