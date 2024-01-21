import typer

app = typer.Typer()

APP_NAME = "pacbundle"


@app.command()
def hello():
    app_dir = typer.get_app_dir(APP_NAME)
    print(f"App dir is {app_dir}")
