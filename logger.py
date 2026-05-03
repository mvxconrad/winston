import csv
from datetime import datetime
from pathlib import Path


class Logger:
    def __init__(self, path="logs/raw/observations.csv"):
        self.path = Path(path)
        # Make sure the parent directory exists
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wrote_header = self.path.exists() # if file already exists, assume header is written
        self._file = open(self.path, "a", newline="")
        self._writer = csv.writer(self._file)

    def log(self, sections):
        # Build a row: timestamp, then values from each section
        row = [datetime.now().isoformat()]
        for section in sections:
            row.extend(section.csv_columns())

        if not self._wrote_header:
            header = ["timestamp"]
            for section in sections:
                header.extend(section.csv_headers())
            self._writer.writerow(header)
            self._wrote_header = True

        self._writer.writerow(row)
        self._file.flush() # make sure it hits disk

    def close(self):
        self._file.close()