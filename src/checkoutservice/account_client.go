package main

import (
	"context"
	"os"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/checkoutservice/genproto"
)

// accountClient is checkoutservice's narrow view of accountservice: resolve the caller's session to a
// customer id (so the customer is never taken from the request), and resolve a saved payment method to
// its opaque demo-PSP token (service-only RPC).
type accountClient struct {
	c      pb.AccountServiceClient
	secret string
}

type tokenInfo struct{ token, brand, last4 string }

func newAccountClient(conn *grpc.ClientConn) *accountClient {
	if conn == nil {
		return nil
	}
	return &accountClient{c: pb.NewAccountServiceClient(conn), secret: os.Getenv("ACCOUNT_SERVICE_SECRET")}
}

func (a *accountClient) ctx(ctx context.Context, token string) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	md := metadata.Pairs("x-service-name", "checkoutservice", "x-service-secret", a.secret)
	if token != "" {
		md.Append("x-account-session", token)
	}
	return metadata.NewOutgoingContext(ctx, md), cancel
}

func (a *accountClient) resolve(ctx context.Context, token string) (string, error) {
	cctx, cancel := a.ctx(ctx, token)
	defer cancel()
	r, err := a.c.ResolveSession(cctx, &pb.Empty{})
	if err != nil || !r.GetAuthenticated() {
		return "", err
	}
	return r.GetCustomerId(), nil
}

func (a *accountClient) resolveToken(ctx context.Context, customerID, methodID string) (tokenInfo, error) {
	cctx, cancel := a.ctx(ctx, "")
	defer cancel()
	r, err := a.c.ResolvePaymentMethodToken(cctx, &pb.PaymentMethodRef{Id: methodID, CustomerId: customerID})
	if err != nil {
		return tokenInfo{}, err
	}
	return tokenInfo{token: r.GetToken(), brand: r.GetBrand(), last4: r.GetLast4()}, nil
}
