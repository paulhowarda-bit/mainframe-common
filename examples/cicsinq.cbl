      *================================================================*
      * CICSINQ - a CICS/Db2 inquiry transaction. Exercises the         *
      * preprocessor (COPY) and embedded-language extraction:           *
      *   * COPY CUSTREC pulls in the customer record + its 88-levels    *
      *     (data the model would otherwise never see).                  *
      *   * EXEC SQL SELECT ... captures host variables.                 *
      *   * EXEC CICS LINK = call-return; XCTL = transfer-out;           *
      *     RETURN = terminate; HANDLE CONDITION = implicit handler.     *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. CICSINQ.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-EOF              PIC X VALUE 'N'.
       COPY CUSTREC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC CICS HANDLE CONDITION
               NOTFND(8000-NOTFOUND)
           END-EXEC
           PERFORM 1000-LOOKUP
           IF CUST-ACTIVE
               PERFORM 2000-POST
           ELSE
               EXEC CICS XCTL PROGRAM('CLOSEDPG')
               END-EXEC
           END-IF
           EXEC CICS RETURN
           END-EXEC.
       1000-LOOKUP.
           EXEC SQL
               SELECT NAME, BAL INTO :CUST-NAME, :CUST-BALANCE
               FROM CUST WHERE ID = :CUST-ID
           END-EXEC.
       2000-POST.
           EXEC CICS LINK PROGRAM('POSTLOG')
           END-EXEC
           ADD 1 TO CUST-BALANCE.
       8000-NOTFOUND.
           DISPLAY 'NOT FOUND'
           EXEC CICS RETURN
           END-EXEC.
