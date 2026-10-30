EduNexus - Student Guide (your own laptop)
============================================

EduNexus runs completely on YOUR laptop. Your records are stored only on your laptop.
Nothing is sent to a server, and nobody else can see your data.


BEFORE YOU START (one time only)
--------------------------------
You need Python 3.11 or newer. You do NOT need Node.js and you do NOT need to edit any file.

1. Install Python from https://www.python.org/downloads/
2. During installation, TICK "Add python.exe to PATH" on the first screen, then click Install.


1. EXTRACT THE ZIP
------------------
- Right-click EduNexus_student.zip and choose "Extract All...".
- Extract it to a normal folder such as C:\EduNexus (not inside the ZIP viewer, not on a USB stick).
- Keep the whole folder together. Do not move single files out of it.


2. START EDUNEXUS
-----------------
- Open the EduNexus folder and double-click  Start_EduNexus.bat
- The FIRST start takes a few minutes: it installs the components EduNexus needs.
  Later starts take a few seconds.
- A black window stays open while EduNexus runs. Keep it open while you work.
- Your web browser opens by itself at  http://127.0.0.1:8001
  (If it does not open, copy that address into your browser.)
- If port 8001 is busy, EduNexus automatically uses 8002 (or the next free number).
  The window shows the address to use.


3. SIGN IN
----------
Demo accounts (fictional, for practice). Use them to explore the system:

  Administrator : admin@edunexus.test          password: AdminPass123!
  Faculty       : faculty@edunexus.test        password: FacultyPass123!
  Student       : student@edunexus.test        password: StudentPass123!
  Student       : student.two@edunexus.test    password: StudentPass123!

Curriculum demo (8 semesters, sections A and B):
  Students  : cse1a.01@edunexus.in, cse1a.02@edunexus.in ... (the same pattern for every semester and section)
  Faculty   : cse.faculty.dsa@edunexus.in and other cse.faculty.* addresses
  Password for all curriculum accounts: EduDemo2026!

These accounts are for demonstration only. Your laptop's copy is independent of every other laptop.


4. USE EDUNEXUS
---------------
Students: attendance, marks, quizzes, subjects, materials, assignments, announcements, previous papers.
Faculty : record attendance and marks, upload materials and question papers, publish assignments.
Admin   : approve students, manage subjects and allocations, view audit history.

Everything you save is stored immediately on this laptop. Refreshing the page, signing out,
closing the browser, or restarting EduNexus does not lose data.


5. STOP EDUNEXUS SAFELY
-----------------------
- Preferred: double-click  Stop_EduNexus.bat
- Or close the black EduNexus window, or press Ctrl+C in it.
- Wait for the message "EduNexus has stopped. Your data is saved."


6. BACK UP YOUR DATA (do this regularly, for example every week and before any update)
--------------------------------------------------------------------------------------
- Double-click  Backup_EduNexus.bat
- It creates a new dated folder inside the  backups  folder, for example backups\manual-20261031-101500.
- Each backup is checked automatically. Older backups are never overwritten.
- Copy the most recent backup folder to a USB drive or another safe place.
- You can run a backup while EduNexus is open.


7. RESTORE A BACKUP (only when you need to)
-------------------------------------------
- CLOSE EduNexus first (Stop_EduNexus.bat).
- Double-click  Restore_EduNexus.bat
- Pick the backup from the numbered list.
- Type  RESTORE  (in capitals) and press Enter to confirm.
- Before anything is replaced, your current data is backed up automatically into backups\before-restore-...
  Nothing is lost by a restore: you can restore that backup again later.
- Start EduNexus again with Start_EduNexus.bat.


8. UPDATE EDUNEXUS TO A NEWER VERSION
-------------------------------------
- CLOSE EduNexus first.
- Extract the NEW package into its own folder (for example C:\EduNexus-new).
- Double-click  Update_EduNexus.bat  and paste the path of the NEW folder.
- Your attendance, marks, quiz results, students, uploads and papers are kept.
  A backup is made first.
- Start EduNexus with Start_EduNexus.bat.


9. AI TUTOR AND SOCRATIC LEARNING
---------------------------------
The AI Tutor and Socratic Learning answer boxes need a Google Gemini key that is set up by the
person who installs EduNexus for the class (it is never included in the ZIP). Without it, these
answer boxes stay disabled and explain why. Course materials, attendance, marks, quizzes and
downloads work without it.


10. IF SOMETHING GOES WRONG
---------------------------
- The window says Python was not found: install Python 3.11+ (step "Before you start") and run again.
- "Ports ... are all in use": close other programs that use local web servers, then start again.
- EduNexus did not start: open the  logs  folder and read  backend.log  and  install.log.
  Do not share these files publicly; they can contain personal information.
- Never delete the  backend\data  folder, the  backups  folder, or  backend\.env.
  backend\data holds your database and uploaded files. backend\.env holds this laptop's private settings.
- Do not edit Python or other program files. Use Update_EduNexus.bat for new versions.


WHAT IS KEPT WHERE
------------------
- Database ............ backend\data\edunexus.db
- Uploaded files ...... backend\data\uploads
- Backups ............. backups\
- Private settings .... backend\.env  (created automatically on first start)
- Logs ................ logs\
