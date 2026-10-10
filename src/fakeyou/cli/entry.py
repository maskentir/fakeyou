from fakeyou.settings import export_dotenv


def main() -> None:
    export_dotenv()
    from fakeyou.cli.main import main as run_cli

    run_cli()
