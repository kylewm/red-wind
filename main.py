
from app import app
from models import *
from views import *
from shortlinks import *


if __name__ == '__main__':
    app.run(debug=True, threaded=True)
