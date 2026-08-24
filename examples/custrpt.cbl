      *================================================================*
      * CUSTRPT - canonical sequential batch read loop.                *
      * Mirrors the worked example in cobol-to-statecharts.md so the   *
      * recovered statechart can be compared against the reference.    *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. CUSTRPT.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT CUST-FILE ASSIGN TO CUSTIN
               ORGANIZATION IS SEQUENTIAL.
       DATA DIVISION.
       FILE SECTION.
       FD  CUST-FILE.
       01  CUST-REC.
           05  CUST-AMT        PIC 9(7)V99 COMP-3.
       WORKING-STORAGE SECTION.
       01  WS-EOF              PIC X VALUE 'N'.
           88  END-OF-FILE     VALUE 'Y'.
       01  WS-TOTAL            PIC 9(11)V99 VALUE ZERO.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-INIT
           PERFORM 2000-PROCESS UNTIL WS-EOF = 'Y'
           PERFORM 3000-TERM
           STOP RUN.
       1000-INIT.
           OPEN INPUT CUST-FILE
           READ CUST-FILE
               AT END MOVE 'Y' TO WS-EOF
           END-READ.
       2000-PROCESS.
           ADD CUST-AMT TO WS-TOTAL
           READ CUST-FILE
               AT END MOVE 'Y' TO WS-EOF
           END-READ.
       3000-TERM.
           CLOSE CUST-FILE
           DISPLAY WS-TOTAL.
