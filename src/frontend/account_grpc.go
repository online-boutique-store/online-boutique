package main

import (
	"context"
	"net/http"
	"time"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/frontend/genproto"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const accountCallTimeout = 4 * time.Second

// accountGRPC talks to accountservice. Every call carries this service's credential; user calls
// also carry the opaque account session (HttpOnly cookie). Customer ids are never sent.
type accountGRPC struct {
	client pb.AccountServiceClient
	secret string
}

func newAccountGRPC(conn *grpc.ClientConn, secret string) *accountGRPC {
	return &accountGRPC{client: pb.NewAccountServiceClient(conn), secret: secret}
}

// ctx builds the outgoing context: parent request context (trace + allowlisted baggage) plus
// credentials, bounded by accountCallTimeout.
func (a *accountGRPC) ctx(r *http.Request) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithTimeout(r.Context(), accountCallTimeout)
	md := metadata.Pairs("x-service-name", "frontend", "x-service-secret", a.secret)
	if tok := cookieValue(r, cookieAuth); tok != "" {
		md.Append("x-account-session", tok)
	}
	return metadata.NewOutgoingContext(ctx, md), cancel
}

type fieldErr struct{ Code, Message string }

// accountErr is a decoded accountservice failure. Messages come from the shared contract / server;
// nothing here echoes user input.
type accountErr struct {
	Code   codes.Code
	Reason string
	Fields map[string]fieldErr
}

func (e *accountErr) Error() string { return e.Code.String() + ":" + e.Reason }

func (e *accountErr) unavailable() bool {
	return e.Code == codes.Unavailable || e.Code == codes.DeadlineExceeded
}

func decodeAccountErr(err error) *accountErr {
	if err == nil {
		return nil
	}
	st, ok := status.FromError(err)
	if !ok {
		return &accountErr{Code: codes.Unavailable, Reason: "unreachable"}
	}
	out := &accountErr{Code: st.Code(), Fields: map[string]fieldErr{}}
	for _, d := range st.Details() {
		switch v := d.(type) {
		case *errdetails.BadRequest:
			for _, fv := range v.GetFieldViolations() {
				out.Fields[fv.GetField()] = fieldErr{Code: fv.GetReason(), Message: fv.GetDescription()}
			}
		case *errdetails.ErrorInfo:
			out.Reason = v.GetReason()
		}
	}
	if out.Reason == "" && st.Message() != "" {
		out.Reason = st.Message()
	}
	return out
}
