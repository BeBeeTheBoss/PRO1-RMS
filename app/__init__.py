
from flask import Flask

def create_app():

    app = Flask(__name__)

    app.config["SECRET_KEY"] = "change-this-to-a-random-secret"

    from app.routes import main
    from app.candidate_routes import candidate_bp

    app.register_blueprint(main)
    app.register_blueprint(candidate_bp)

    # Notification Routes
    from app.notification_routes import notification_bp
    app.register_blueprint(notification_bp)

    # Exam Management Routes
    from app.exam_routes import exam_bp
    app.register_blueprint(exam_bp)

    # Start Interview Reminder Scheduler
    from app.interview_reminders import start_interview_scheduler
    start_interview_scheduler(app)

    return app
