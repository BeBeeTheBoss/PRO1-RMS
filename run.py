
from app import create_app
from dotenv import load_dotenv


load_dotenv()

app = create_app()


if __name__ == "__main__":

    app.run(
        host="192.168.200.230",
        port=5000,
        debug=True,
        use_reloader=False
    )
