      *================================================================*
      * ACCUM - PERFORM ... UNTIL exercising call-return + context     *
      * threading with no file I/O, so the emitted machine runs end-to-*
      * end under stock XState. WS-I -> 1..5, WS-SUM -> 1+2+3+4+5 = 15. *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. ACCUM.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-I      PIC 9(4) VALUE ZERO.
       01  WS-SUM    PIC 9(6) VALUE ZERO.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-STEP UNTIL WS-I = 5
           STOP RUN.
       1000-STEP.
           ADD 1 TO WS-I
           ADD WS-I TO WS-SUM.
