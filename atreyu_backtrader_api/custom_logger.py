import logging

from datetime import datetime
import os

# Create the global logger
def setup_custom_logger(global_name, filename, logdirname = './logs', debug_level = logging.ERROR, console_level = logging.ERROR, console = False):
  
  # Check if logdir exists
  if not os.path.exists(logdirname):
    # Create it if not there 
    try:
      os.mkdir(logdirname)
    except OSError:
      print(f"Creation log_dir: {logdirname} failed")
      return None
    else:
      print (f"Created log_dir: {logdirname}")

  # Check if the file exists
  logfilename = os.path.join(logdirname, filename)
  if os.path.exists(logfilename):
    # Create a new logfile for every run
    now = datetime.now().strftime('%Y%m%dT%H%M%S')
    new_logfile = os.path.join(logdirname, now + "." + filename)
    os.rename(logfilename, new_logfile)

  # Set up a log line format
  log_fmt  = '%(asctime)s.%(msecs)03d - %(levelname)-7s - %(threadName)s - %(filename)s:%(lineno)d [%(funcName)s] - %(message)s'
  date_fmt = '%Y-%m-%d %H:%M:%S'
  logging.basicConfig(filename=logfilename, level=debug_level, format=log_fmt, datefmt=date_fmt)

  logger = logging.getLogger(global_name)
  logger.setLevel(debug_level)

  if console:
    # Create console handler and set debug_level
    ch = logging.StreamHandler()
    ch.setLevel(console_level)

    # Create formatter
    formatter = logging.Formatter(fmt=log_fmt, datefmt=date_fmt)

    # Add formatter to ch
    ch.setFormatter(formatter)
    
    # Add ch to logger
    logger.addHandler(ch)

  return logger
